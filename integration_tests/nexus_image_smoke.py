import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from gzip import compress
from hashlib import sha1
from pathlib import Path
from queue import SimpleQueue
from threading import Barrier, BrokenBarrierError
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import django
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.urls import resolve, reverse
from django.utils.timezone import now


def run_concurrently(function, jobs):
    from django.db import connections

    start = Barrier(len(jobs))

    def run(job):
        start.wait(timeout=10)
        try:
            return function(*job)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        return list(executor.map(run, jobs))


def check_parallel_symbol_uploads(binaries, test_name):
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import Client

    from apps.api_tokens.models import APIToken
    from apps.difs.models import DebugInformationFile
    from apps.difs.release_symbols import assemble_tracked_upload
    from apps.difs.tasks import difs_get_file_from_chunks
    from apps.files.models import File, FileBlob
    from apps.organizations_ext.models import Organization
    from apps.projects.models import Project
    from apps.releases.models import Release

    name = f"Nexus CI {test_name}"
    organization = Organization.objects.create(name=name)
    project = Project.objects.create(name=name, organization=organization)
    owner = get_user_model().objects.create_user(f"{project.slug}@example.com", None)
    organization.add_user(owner)
    token = APIToken.objects.create(user=owner)
    token.add_permission("project:releases")
    release_version = "ci-release"
    targets = {"linux-first": binaries[0], "linux-second": binaries[1]}
    checksums = {
        target: sha1(content).hexdigest() for target, content in targets.items()
    }
    auth = {"Authorization": f"Bearer {token.token}"}
    assemble_url = reverse(
        "api:difs_assemble_api", args=[organization.slug, project.slug]
    )
    chunk_url = reverse("api:chunk_upload", args=[organization.slug])
    upload_id = "1-1"
    callbacks = SimpleQueue()

    async def enqueue(*args):
        callbacks.put(args)

    def assemble(target, checksum):
        payload = {checksum: {"name": "ci-symbols", "chunks": [checksum]}}
        response = Client().post(
            assemble_url,
            data=payload,
            content_type="application/json",
            headers=auth
            | {
                "X-GlitchTip-Release": release_version,
                "X-GlitchTip-Build-Target": target,
                "X-GlitchTip-Upload-Id": upload_id,
            },
        )
        assert response.status_code == 200, response.content
        return response.json()[checksum]

    def upload_chunk(checksum, content):
        response = Client().post(
            chunk_url,
            {"file_gzip": SimpleUploadedFile(checksum, compress(content))},
            headers=auth,
        )
        assert response.status_code == 200, response.content

    shared_checksum = (
        next(iter(checksums.values())) if len(set(checksums.values())) == 1 else None
    )
    concurrent_storage = Barrier(2)
    concurrent_missing_file = Barrier(2)
    storage = FileBlob._meta.get_field("blob").storage
    save_file = storage._save

    def wait_for_second_writer(gate):
        # The checksum lock admits only one writer. Without it, let both
        # writers observe the miss before either can save.
        try:
            gate.wait(timeout=1)
        except BrokenBarrierError:
            pass

    def save_chunk(name, content):
        if Path(name).name == shared_checksum:
            wait_for_second_writer(concurrent_storage)
        return save_file(name, content)

    def lookup_file(checksum, chunks):
        file = difs_get_file_from_chunks(checksum, chunks)
        if file is None and checksum == shared_checksum:
            wait_for_second_writer(concurrent_missing_file)
        return file

    jobs = list(checksums.items())
    with patch(
        "apps.difs.api.assemble_tracked_upload", SimpleNamespace(aenqueue=enqueue)
    ):
        preflight = run_concurrently(assemble, jobs)
        assert preflight == [
            {"state": "not_found", "missingChunks": [checksum]} for _, checksum in jobs
        ]
        assert callbacks.empty()
        with patch.object(storage, "_save", save_chunk):
            run_concurrently(
                upload_chunk, [(checksum, targets[target]) for target, checksum in jobs]
            )
        accepted = run_concurrently(assemble, jobs)
        assert accepted == [{"state": "created", "missingChunks": []}] * len(jobs)

        tasks = [callbacks.get_nowait() for _ in jobs]
        assert callbacks.empty()
        with patch("apps.difs.tasks.difs_get_file_from_chunks", lookup_file):
            run_concurrently(assemble_tracked_upload.call, tasks)

        release = Release.objects.get(
            organization=organization, version=release_version
        )
        attempts = release.data["debug_symbol_uploads"][project.slug]
        manifests = release.data["debug_symbols"][project.slug]
        for target, checksum in jobs:
            assert attempts[target][upload_id] == {
                "checksums": [checksum],
                "complete": True,
            }
            symbols = manifests[target]
            assert {symbol["sha1"] for symbol in symbols} == {checksum}
            assert all(symbol["debug_id"] for symbol in symbols)

            blob = FileBlob.objects.get(checksum=checksum)
            path = Path(blob.blob.path)
            assert path.read_bytes() == targets[target]
            assert list(path.parent.glob(f"{checksum}*")) == [path]
            artifact = File.objects.get(checksum=checksum)
            assert artifact.blob_id == blob.pk
            assert (
                DebugInformationFile.objects.filter(
                    project=project, file=artifact
                ).count()
                == 1
            )

        completed = run_concurrently(assemble, jobs)
        assert completed == [{"state": "ok", "missingChunks": []}] * len(jobs)
        assert callbacks.empty()


def check_cleanup_api(organization, other_organization):
    from django.contrib.auth import get_user_model
    from django.test import Client

    from apps.api_tokens.models import APIToken

    owner = get_user_model().objects.create_user("cleanup-ci@example.com", None)
    organization.add_user(owner)
    admin_token = APIToken.objects.create(user=owner)
    admin_token.add_permission("org:admin")
    release_token = APIToken.objects.create(user=owner)
    release_token.add_permission("project:releases")

    client = Client()

    def request(organization, token=None):
        url = reverse(
            "api:cleanup_organization_release_files", args=[organization.slug]
        )
        headers = {"Authorization": f"Bearer {token.token}"} if token else {}
        return client.post(url, headers=headers)

    enqueue = AsyncMock()
    with patch(
        "apps.releases.api.cleanup_organization_release_files_task",
        SimpleNamespace(aenqueue=enqueue),
    ):
        assert request(organization).status_code == 401
        assert request(organization, release_token).status_code == 403
        assert request(other_organization, admin_token).status_code == 404
        enqueue.assert_not_awaited()

        accepted = request(organization, admin_token)
        assert accepted.status_code == 202, accepted.content
        assert accepted.content == b""
        enqueue.assert_awaited_once_with(organization.pk)


def check_release_cleanup():
    from apps.difs.models import DebugInformationFile
    from apps.files.models import File, FileBlob
    from apps.organizations_ext.models import Organization
    from apps.projects.models import Project
    from apps.releases.models import Release
    from apps.releases.tasks import cleanup_organization_release_files_task
    from apps.sourcecode.models import DebugSymbolBundle

    def release_file(name):
        artifact = File.objects.create(name=name, type="release.file")
        artifact.putfile(ContentFile(name.encode()))
        assert Path(artifact.blob.blob.path).read_bytes() == name.encode()
        return artifact

    def releases(organization):
        published = now()
        return [
            Release.objects.create(
                organization=organization,
                version=f"ci-{number}",
                released=published + timedelta(days=number - 3),
            )
            for number in range(4)
        ]

    organization = Organization.objects.create(name="Nexus Cleanup")
    other_organization = Organization.objects.create(name="Nexus Other Cleanup")
    expired, release_without_metadata, pending_release, retained = releases(
        organization
    )
    other_expired, _, _, other_retained = releases(other_organization)

    obsolete_file = release_file("expired.js")
    other_obsolete_file = release_file("other-expired.js")
    shared_map = release_file("shared.js.map")
    retained_file = release_file("retained.js")
    obsolete_alias = File.objects.create(
        name="obsolete-copy.js", blob=retained_file.blob
    )
    obsolete = DebugSymbolBundle.objects.create(
        organization=organization,
        release=expired,
        file=obsolete_file,
        sourcemap_file=shared_map,
    )
    alias_bundle = DebugSymbolBundle.objects.create(
        organization=organization, release=expired, file=obsolete_alias
    )
    keep = DebugSymbolBundle.objects.create(
        organization=organization, release=retained, file=retained_file
    )
    other_obsolete = DebugSymbolBundle.objects.create(
        organization=other_organization, release=other_expired, file=other_obsolete_file
    )
    other_keep = DebugSymbolBundle.objects.create(
        organization=other_organization,
        release=other_retained,
        file=release_file("other-retained.js"),
        sourcemap_file=shared_map,
    )

    project = Project.objects.create(name="Native Cleanup", organization=organization)
    for release in (expired, release_without_metadata, pending_release, retained):
        release.projects.add(project)

    def native_symbol(name, *, debug_id=None, file=None, arch="x86_64"):
        return DebugInformationFile.objects.create(
            project=project,
            name=name,
            file=file if file is not None else release_file(name),
            data={"debug_id": debug_id or str(uuid4()), "arch": arch},
        )

    def symbol_entries(*symbols):
        return [symbol.data | {"sha1": symbol.file.checksum} for symbol in symbols]

    old_native = native_symbol("expired.debug")
    shared_symbol = native_symbol("old-shared-id.debug")
    retained_native = native_symbol(
        "retained.debug", debug_id=shared_symbol.data["debug_id"]
    )
    unrecorded_arch = native_symbol(
        "unrecorded-arch.debug", file=retained_native.file, arch="arm64"
    )
    pending_native = native_symbol("still-uploading.debug")
    unassociated_native = native_symbol("no-release.debug")
    expired.data = {
        "debug_symbols": {
            project.slug: {
                "former-target": symbol_entries(
                    old_native, shared_symbol, unrecorded_arch, pending_native
                )
            }
        }
    }
    expired.save(update_fields=["data"])
    pending_release.data = {
        "debug_symbol_uploads": {
            project.slug: {
                "new-target": {
                    "1-1": {
                        "checksums": [pending_native.file.checksum],
                        "complete": False,
                    }
                }
            }
        }
    }
    pending_release.save(update_fields=["data"])
    retained.data = {
        "debug_symbols": {
            project.slug: {"another-target": symbol_entries(retained_native)}
        }
    }
    retained.save(update_fields=["data"])

    def check_artifact(artifact, exists):
        assert File.objects.filter(pk=artifact.pk).exists() == exists, artifact.name
        assert FileBlob.objects.filter(pk=artifact.blob_id).exists() == exists, (
            artifact.name
        )
        assert Path(artifact.blob.blob.path).exists() == exists, artifact.name

    check_cleanup_api(organization, other_organization)
    cleanup_organization_release_files_task.call(organization.pk)
    assert not DebugSymbolBundle.objects.filter(
        pk__in=[obsolete.pk, alias_bundle.pk]
    ).exists()
    assert not File.objects.filter(pk=obsolete_alias.pk).exists()
    check_artifact(obsolete_file, exists=False)
    assert not DebugInformationFile.objects.filter(pk=old_native.pk).exists()
    check_artifact(old_native.file, exists=False)
    assert DebugSymbolBundle.objects.filter(pk=other_obsolete.pk).exists()
    check_artifact(other_obsolete_file, exists=True)

    call_command("cleanup_release_files")
    assert not DebugSymbolBundle.objects.filter(pk=other_obsolete.pk).exists()
    check_artifact(other_obsolete_file, exists=False)
    assert Release.objects.filter(pk__in=[expired.pk, other_expired.pk]).count() == 2
    assert (
        DebugSymbolBundle.objects.filter(pk__in=[keep.pk, other_keep.pk]).count() == 2
    )
    check_artifact(retained_file, exists=True)
    check_artifact(shared_map, exists=True)
    for native in (
        shared_symbol,
        retained_native,
        unrecorded_arch,
        pending_native,
        unassociated_native,
    ):
        assert DebugInformationFile.objects.filter(pk=native.pk).exists(), native.name
        check_artifact(native.file, exists=True)


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
    django.setup()

    call_command("check")
    call_command("init_database", verbosity=0)
    assert Path("/code/dist/index.html").is_file(), "Frontend assets are missing"
    route = resolve("/api/0/projects/nexus-ci/nexus-ci/files/difs/assemble/")
    assert route.url_name == "difs_assemble_api"

    binary = Path("/tmp/glitchtip-test-symbols").read_bytes()
    check_parallel_symbol_uploads((binary, binary), "shared symbols")
    check_parallel_symbol_uploads(
        (binary + b"\x00first", binary + b"\x00second"), "distinct symbols"
    )

    check_release_cleanup()


if __name__ == "__main__":
    main()
