import os
from hashlib import sha1
from pathlib import Path

import django
from django.core.files import File
from django.core.management import call_command
from django.urls import resolve


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
    call_command("migrate", interactive=False, verbosity=0)
    assert Path("/code/dist/index.html").is_file(), "Frontend assets are missing"
    route = resolve("/api/0/projects/nexus-ci/nexus-ci/files/difs/assemble/")
    assert route.url_name == "difs_assemble_api"

    organization = Organization.objects.create(name="Nexus CI")
    project = Project.objects.create(name="Nexus CI", organization=organization)
    binary = Path("/tmp/glitchtip-test-symbols")
    checksum = sha1(binary.read_bytes()).hexdigest()
    with binary.open("rb") as source:
        FileBlob.objects.create(
            blob=File(source, name=binary.name),
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


if __name__ == "__main__":
    main()
