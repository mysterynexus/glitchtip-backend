"""Associate completed native debug uploads with a release."""

import re
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.db import connection, transaction
from django.db.models import F
from django.tasks import task
from ninja.errors import HttpError

from apps.projects.models import Project
from apps.releases.models import Release

from .models import DebugInformationFile

SHA1 = re.compile(r"[0-9a-f]{40}")
ATTEMPT_ID = re.compile(r"[1-9][0-9]*-[1-9][0-9]*")


@dataclass(frozen=True)
class TrackedUpload:
    version: str
    target: str
    attempt_id: str


def read_tracked_upload(headers) -> TrackedUpload | None:
    version = headers.get("X-GlitchTip-Release")
    target = headers.get("X-GlitchTip-Build-Target")
    attempt_id = headers.get("X-GlitchTip-Upload-Id")
    if version is None and target is None and attempt_id is None:
        return None

    max_length = Release._meta.get_field("version").max_length
    if not version or version != version.strip() or len(version) > max_length:
        raise HttpError(422, "A valid X-GlitchTip-Release is required")
    if not target or not target.strip():
        raise HttpError(422, "A nonempty X-GlitchTip-Build-Target is required")
    if not attempt_id or len(attempt_id) > 63 or not ATTEMPT_ID.fullmatch(attempt_id):
        raise HttpError(
            422, "X-GlitchTip-Upload-Id must be a CI run ID and attempt number"
        )
    return TrackedUpload(version, target, attempt_id)


def child_object(mapping, key):
    child = mapping.setdefault(key, {})
    if not isinstance(child, dict):
        raise HttpError(409, f"Release metadata at {key} must be an object")
    return child


def symbols_for_checksums(project, checksums):
    rows = (
        DebugInformationFile.objects.filter(
            project=project,
            file__checksum__in=checksums,
            file__blob__checksum=F("file__checksum"),
        )
        .exclude(file__blob__blob="")
        .values_list("data__debug_id", "data__arch", "file__checksum")
    )
    return {
        (debug_id, arch, checksum)
        for debug_id, arch, checksum in rows
        if isinstance(debug_id, str)
        and debug_id
        and (arch is None or isinstance(arch, str))
    }


def merge_symbols(data, project_slug, target, symbols):
    targets = child_object(child_object(data, "debug_symbols"), project_slug)
    prior = targets.get(target, [])
    if not isinstance(prior, list):
        raise HttpError(409, "Existing target debug symbol manifest is malformed")
    try:
        merged = symbols | {
            (item["debug_id"], item["arch"], item["sha1"]) for item in prior
        }
        ordered = sorted(merged, key=lambda item: (item[0], item[1] or "", item[2]))
    except (KeyError, TypeError) as error:
        raise HttpError(
            409, "Existing target debug symbol manifest is malformed"
        ) from error
    targets[target] = [
        {"debug_id": debug_id, "arch": arch, "sha1": checksum}
        for debug_id, arch, checksum in ordered
    ]
    return targets[target] != prior


@transaction.atomic
def track_upload(
    project: Project,
    upload: TrackedUpload,
    requested: set[str],
    *,
    release_id: int | None = None,
) -> tuple[set[str], bool, int]:
    if not requested or any(not SHA1.fullmatch(checksum) for checksum in requested):
        raise HttpError(422, "Tracked uploads require a set of SHA-1 checksums")

    # Cleanup takes the Release lock first, so it can never read before this
    # registration commits and then remove a file that we just associated.
    with connection.cursor() as cursor:
        table = connection.ops.quote_name(Release._meta.db_table)
        cursor.execute(f"LOCK TABLE {table} IN ROW EXCLUSIVE MODE")

    release_fields = {
        "organization_id": project.organization_id,
        "version": upload.version,
    }
    releases = Release.objects.select_for_update()
    if release_id is None:
        release, _ = releases.get_or_create(**release_fields)
    else:
        release = releases.filter(pk=release_id, **release_fields).first()
        if release is None:
            return set(), False, release_id
    if not isinstance(release.data, dict):
        raise HttpError(409, "Release data must be an object to store debug symbols")

    attempts = release.data
    for key in ("debug_symbol_uploads", project.slug, upload.target):
        if release_id is None:
            attempts = child_object(attempts, key)
        else:
            attempts = attempts.get(key)
            if not isinstance(attempts, dict):
                return set(), False, release_id
    new_attempt = upload.attempt_id not in attempts
    if new_attempt and release_id is not None:
        return set(), False, release_id

    run, _, ordinal = upload.attempt_id.partition("-")
    retry_number = int(ordinal)
    retries = {
        attempt_id: (int(attempt_id.partition("-")[2]), attempt["complete"])
        for attempt_id, attempt in attempts.items()
        if attempt_id.startswith(run + "-")
        and len(attempt_id) <= 63
        and ATTEMPT_ID.fullmatch(attempt_id)
        and isinstance(attempt, dict)
        and isinstance(attempt.get("complete"), bool)
    }
    if new_attempt:
        if any(
            number > retry_number and complete for number, complete in retries.values()
        ):
            raise HttpError(
                409, "A later retry already completed this debug symbol upload"
            )
        attempts[upload.attempt_id] = {
            "checksums": sorted(requested),
            "complete": False,
        }
    attempt = attempts[upload.attempt_id]
    if not isinstance(attempt, dict) or not isinstance(attempt.get("complete"), bool):
        raise HttpError(409, "Existing debug symbol upload attempt is malformed")
    checksums = attempt.get("checksums")
    if (
        not isinstance(checksums, list)
        or not checksums
        or any(
            not isinstance(checksum, str) or not SHA1.fullmatch(checksum)
            for checksum in checksums
        )
    ):
        raise HttpError(409, "Existing debug symbol checksums are malformed")
    expected = set(checksums)
    if not requested <= expected:
        if release_id is not None:
            return set(), False, release_id
        raise HttpError(
            409, "Upload attempt includes checksums outside its original request"
        )

    symbols = symbols_for_checksums(project, expected)
    ready = {checksum for _, _, checksum in symbols}
    complete = ready == expected
    changed = new_attempt
    if complete:
        merged = merge_symbols(release.data, project.slug, upload.target, symbols)
        changed = changed or merged or not attempt["complete"]
        attempt["complete"] = True
        # A successful retry supersedes failed attempts of the same CI run.
        for attempt_id, (number, was_complete) in retries.items():
            if number < retry_number and not was_complete:
                del attempts[attempt_id]
                changed = True
    if changed:
        release.save(update_fields=["data"])
    if new_attempt:
        release.projects.add(project)
    return ready, complete, release.pk


@task
async def assemble_tracked_upload(
    project_id,
    name,
    checksum,
    chunks,
    debug_id,
    release_id,
    version,
    target,
    attempt_id,
):
    from .tasks import difs_assemble

    await difs_assemble.acall(project_id, name, checksum, chunks, debug_id)
    project = await Project.objects.filter(pk=project_id).afirst()
    if project is not None:
        await sync_to_async(track_upload)(
            project,
            TrackedUpload(version, target, attempt_id),
            {checksum},
            release_id=release_id,
        )
