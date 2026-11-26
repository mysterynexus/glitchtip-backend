"""Prune older GlitchTip release artifacts and reclaim their unreferenced storage."""

import json
import logging
import os
import re
import stat
from collections import defaultdict
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from django.conf import settings
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import connection, transaction
from django.db.models import Exists, F, OuterRef, Q, Window
from django.db.models.functions import Coalesce, RowNumber

from apps.difs.models import DebugInformationFile
from apps.files.models import File, FileBlob
from apps.releases.models import Release
from apps.sourcecode.models import DebugSymbolBundle
from apps.symbolication.models import SymbolicationCache

logger = logging.getLogger(__name__)
RELEASES_TO_KEEP = 3
STORAGE_PREFIXES = {("uploads", "file_blobs"), ("uploads", "symbolcache")}
SHA1 = re.compile(r"[0-9a-fA-F]{40}")


def lock_tables(*models):
    # Keep the release snapshot and the reference checks stable until commit.
    # Reads can continue. Retry the job if another writer holds a lock too long.
    tables = ", ".join(
        connection.ops.quote_name(model._meta.db_table) for model in models
    )
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute(f"LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE")


def storage_path(root, name):
    relative = PurePosixPath(name)
    path = root.joinpath(*relative.parts)
    if (
        relative.parts[:2] not in STORAGE_PREFIXES
        or len(relative.parts) < 3
        or ".." in relative.parts
        or path.resolve() != path
    ):
        raise ValueError(f"Unexpected GlitchTip storage path: {name!r}")
    return path


def file_identity(path):
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Expected a regular file: {path}")
    return [info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def queue_storage_deletions(root, journal, journal_prefix, names):
    entries = {}
    for name in sorted(names):
        identity = file_identity(storage_path(root, name))
        if identity is not None:
            entries[name] = identity
    if not entries:
        return

    # Persist before committing row deletions. On rollback the rows protect
    # their files. If the pod stops after commit, the next run can unlink them.
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=journal, prefix=journal_prefix, delete=False
    ) as temporary:
        path = Path(temporary.name)
        try:
            json.dump(entries, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
            path = path.rename(path.with_suffix(".json"))
            sync_directory(journal)
        except BaseException:
            path.unlink(missing_ok=True)
            raise


def drain_storage_deletions(root, journal, journal_prefix):
    for record in sorted(journal.glob(f"{journal_prefix}*.json")):
        with transaction.atomic():
            lock_tables(FileBlob, SymbolicationCache)
            # A second job may have processed this entry while we waited.
            try:
                entries = json.loads(record.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue

            referenced = set(
                FileBlob.objects.filter(blob__in=entries).values_list("blob", flat=True)
            )
            referenced.update(
                SymbolicationCache.objects.filter(storage_name__in=entries).values_list(
                    "storage_name", flat=True
                )
            )
            removed = 0
            removed_bytes = 0
            changed_directories = set()
            for name in entries.keys() - referenced:
                path = storage_path(root, name)
                # A later upload may have reused the same relative name.
                if file_identity(path) == entries[name]:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    changed_directories.add(path.parent)
                    removed += 1
                    removed_bytes += entries[name][1]

            for directory in changed_directories:
                sync_directory(directory)
            record.unlink()
            sync_directory(journal)
        logger.info("Unlinked %d stored files (%d bytes)", removed, removed_bytes)


def project_symbol_manifests(release_data, project):
    if not isinstance(release_data, dict):
        return None
    manifests = release_data.get("debug_symbols", {})
    if not isinstance(manifests, dict) or not isinstance(
        manifests.get(project, {}), dict
    ):
        return None

    targets = {}
    for target, entries in manifests.get(project, {}).items():
        if not isinstance(entries, list):
            return None
        symbols = set()
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            debug_id, sha1 = entry.get("debug_id"), entry.get("sha1")
            if not isinstance(debug_id, str) or not debug_id:
                return None
            if not isinstance(sha1, str) or not SHA1.fullmatch(sha1):
                return None
            symbols.add((debug_id, sha1))
        targets[target] = symbols
    return targets


def project_symbol_uploads(release_data, project):
    if not isinstance(release_data, dict):
        return None
    uploads = release_data.get("debug_symbol_uploads", {})
    if not isinstance(uploads, dict) or not isinstance(uploads.get(project, {}), dict):
        return None

    targets = {}
    for target, attempts in uploads.get(project, {}).items():
        if not isinstance(attempts, dict) or not attempts:
            return None
        targets[target] = []
        for attempt in attempts.values():
            if not isinstance(attempt, dict):
                return None
            complete = attempt.get("complete")
            checksums = attempt.get("checksums")
            if (
                not isinstance(complete, bool)
                or not isinstance(checksums, list)
                or not checksums
            ):
                return None
            if any(
                not isinstance(checksum, str) or not SHA1.fullmatch(checksum)
                for checksum in checksums
            ):
                return None
            targets[target].append((complete, checksums))
    return targets


def protected_native_symbols(releases, project):
    debug_ids = set()
    checksums = set()
    for data in releases:
        manifests = project_symbol_manifests(data, project)
        uploads = project_symbol_uploads(data, project)
        if manifests is None or uploads is None:
            return None
        for entries in manifests.values():
            for debug_id, checksum in entries:
                debug_ids.add(debug_id)
                checksums.add(checksum)
        # An upload can reuse an existing file before its manifest is written.
        for attempts in uploads.values():
            for _, attempted_checksums in attempts:
                checksums.update(attempted_checksums)
    return debug_ids, checksums


def obsolete_native_symbols(ranked_releases):
    retained = defaultdict(list)
    old_symbols = defaultdict(set)
    old_checksums = defaultdict(set)
    release_rows = ranked_releases.values_list(
        "organization_id", "data", "retention_rank"
    ).iterator()
    for organization_id, data, rank in release_rows:
        if rank <= RELEASES_TO_KEEP:
            retained[organization_id].append(data)
            continue
        projects = data.get("debug_symbols", {}) if isinstance(data, dict) else {}
        if not isinstance(projects, dict):
            continue
        for project in projects:
            manifests = project_symbol_manifests(data, project)
            if not manifests:
                continue
            uploads = project_symbol_uploads(data, project) or {}
            for target, entries in manifests.items():
                old_symbols[organization_id, project].update(entries)
                recorded_checksums = {sha1 for _, sha1 in entries}
                for complete, checksums in uploads.get(target, ()):
                    if complete:
                        old_checksums[organization_id, project].update(
                            recorded_checksums.intersection(checksums)
                        )

    debug_file_ids = []
    file_ids = set()
    for (organization_id, project), symbols in old_symbols.items():
        if not symbols:
            continue
        protected = protected_native_symbols(retained[organization_id], project)
        if protected is None:
            logger.warning(
                "Skipping native symbols for organization %d project %s due to invalid release metadata",
                organization_id,
                project,
            )
            continue
        protected_ids, protected_checksums = protected

        # A kept build can use a different object with the same Debug ID.
        # Complete tracked uploads also cover sibling objects in the same blob.
        obsolete_checksums = old_checksums[organization_id, project]
        candidates = DebugInformationFile.objects.filter(
            project__organization_id=organization_id,
            project__slug=project,
            file__checksum__in={sha1 for _, sha1 in symbols},
        ).values_list("pk", "file_id", "data__debug_id", "file__checksum")
        for debug_file_id, file_id, debug_id, sha1 in candidates.iterator():
            if (
                isinstance(debug_id, str)
                and debug_id not in protected_ids
                and sha1 not in protected_checksums
                and ((debug_id, sha1) in symbols or sha1 in obsolete_checksums)
            ):
                debug_file_ids.append(debug_file_id)
                file_ids.add(file_id)
    return debug_file_ids, file_ids


def prune_release_files(root, journal, journal_prefix, organization_id):
    with transaction.atomic():
        lock_tables(
            Release,
            FileBlob,
            File,
            DebugSymbolBundle,
            DebugInformationFile,
            SymbolicationCache,
        )
        releases = Release.objects.all()
        bundles = DebugSymbolBundle.objects.all()
        if organization_id is not None:
            releases = releases.filter(organization_id=organization_id)
            bundles = bundles.filter(organization_id=organization_id)

        ranked_releases = releases.annotate(
            retention_rank=Window(
                expression=RowNumber(),
                partition_by=F("organization_id"),
                order_by=[Coalesce("released", "created").desc(), F("pk").desc()],
            )
        )
        old_releases = ranked_releases.filter(
            retention_rank__gt=RELEASES_TO_KEEP
        ).values("pk")
        old_bundles = bundles.filter(release_id__in=old_releases)
        # GlitchTip resolves debug IDs across releases and doesn't store the
        # event's debug IDs, so an old bundle with a debug ID can still be used.
        preserved = old_bundles.filter(debug_id__isnull=False).count()
        bundles = old_bundles.filter(debug_id__isnull=True)
        file_ids = {
            file_id
            for pair in bundles.values_list("file_id", "sourcemap_file_id").iterator()
            for file_id in pair
            if file_id is not None
        }
        bundles_deleted, _ = bundles.delete()

        debug_file_ids, native_file_ids = obsolete_native_symbols(ranked_releases)
        debug_files_deleted, _ = DebugInformationFile.objects.filter(
            pk__in=debug_file_ids
        ).delete()
        file_ids.update(native_file_ids)

        # Files and blobs can be shared across organizations.
        files = File.objects.filter(
            ~Exists(DebugSymbolBundle.objects.filter(file_id=OuterRef("pk"))),
            ~Exists(DebugSymbolBundle.objects.filter(sourcemap_file_id=OuterRef("pk"))),
            ~Exists(DebugInformationFile.objects.filter(file_id=OuterRef("pk"))),
            pk__in=file_ids,
        )
        blob_ids = set(files.exclude(blob_id=None).values_list("blob_id", flat=True))
        files_deleted, _ = files.delete()

        blobs = FileBlob.objects.filter(
            ~Exists(File.objects.filter(blob_id=OuterRef("pk"))), pk__in=blob_ids
        )
        caches = SymbolicationCache.objects.filter(
            Q(source_blob_id__in=blobs.values("pk"))
            | Q(secondary_blob_id__in=blobs.values("pk"))
        )
        names = set(blobs.values_list("blob", flat=True))
        names.update(caches.values_list("storage_name", flat=True))
        names.discard("")
        queue_storage_deletions(root, journal, journal_prefix, names)
        caches_deleted, _ = caches.delete()
        blobs_deleted, _ = blobs.delete()

    logger.info(
        "Deleted %d release bundles, %d native debug files, %d file rows, %d blobs, "
        "and %d caches. Kept %d older bundles with debug IDs",
        bundles_deleted,
        debug_files_deleted,
        files_deleted,
        blobs_deleted,
        caches_deleted,
        preserved,
    )


def cleanup_release_files(*, organization_id: int | None = None) -> None:
    """Remove obsolete release artifacts and storage for one or all organizations."""
    # Disk deletion must observe committed row deletions.
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Release file cleanup must run outside a transaction.")

    root = Path(settings.MEDIA_ROOT).resolve()
    blob_storage = FileBlob._meta.get_field("blob").storage
    if (
        not settings.MEDIA_ROOT
        or not isinstance(blob_storage, FileSystemStorage)
        or not isinstance(default_storage, FileSystemStorage)
        or Path(blob_storage.location).resolve() != root
        or Path(default_storage.location).resolve() != root
    ):
        raise RuntimeError(
            "Release file cleanup requires GlitchTip's local media storage."
        )
    if connection.vendor != "postgresql":
        raise RuntimeError("Release file cleanup requires PostgreSQL.")

    journal = root / ".release-file-cleanup"
    journal.mkdir(mode=0o700, exist_ok=True)
    sync_directory(root)
    # An organization cleanup only retries its own disk deletions. The nightly
    # command also retries records left by API calls or the previous script.
    journal_prefix = "" if organization_id is None else f"org-{organization_id}-"
    drain_storage_deletions(root, journal, journal_prefix)
    prune_release_files(root, journal, journal_prefix, organization_id)
    drain_storage_deletions(root, journal, journal_prefix)
