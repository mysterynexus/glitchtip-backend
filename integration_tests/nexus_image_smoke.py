import os
from datetime import timedelta
from hashlib import sha1
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import django
from django.core.files import File as UploadedFile
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.urls import resolve, reverse
from django.utils.timezone import now


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
    from apps.files.models import File, FileBlob
    from apps.organizations_ext.models import Organization
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
    expired, _, _, retained = releases(organization)
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


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
    django.setup()

    from apps.difs.release_symbols import (
        TrackedUpload,
        assemble_tracked_upload,
        track_upload,
    )
    from apps.files.models import FileBlob
    from apps.organizations_ext.models import Organization
    from apps.projects.models import Project
    from apps.releases.models import Release

    call_command("check")
    call_command("init_database", verbosity=0)
    assert Path("/code/dist/index.html").is_file(), "Frontend assets are missing"
    route = resolve("/api/0/projects/nexus-ci/nexus-ci/files/difs/assemble/")
    assert route.url_name == "difs_assemble_api"

    organization = Organization.objects.create(name="Nexus CI")
    project = Project.objects.create(name="Nexus CI", organization=organization)
    binary = Path("/tmp/glitchtip-test-symbols")
    checksum = sha1(binary.read_bytes()).hexdigest()
    with binary.open("rb") as source:
        FileBlob.objects.create(
            blob=UploadedFile(source, name=binary.name),
            checksum=checksum,
            size=binary.stat().st_size,
        )

    upload = TrackedUpload(
        version="ci-release", target="server-linux", attempt_id="1-1"
    )
    ready, complete, release_id = track_upload(project, upload, {checksum})
    assert not ready and not complete
    assemble_tracked_upload.call(
        project.id,
        binary.name,
        checksum,
        [checksum],
        None,
        release_id,
        upload.version,
        upload.target,
        upload.attempt_id,
    )
    release = Release.objects.get(pk=release_id)
    attempt = release.data["debug_symbol_uploads"][project.slug][upload.target][
        upload.attempt_id
    ]
    assert attempt == {"checksums": [checksum], "complete": True}
    symbols = release.data["debug_symbols"][project.slug][upload.target]
    assert any(symbol["sha1"] == checksum and symbol["debug_id"] for symbol in symbols)

    check_release_cleanup()


if __name__ == "__main__":
    main()
