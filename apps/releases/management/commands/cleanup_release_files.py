from django.core.management.base import BaseCommand

from apps.releases.file_cleanup import cleanup_release_files


class Command(BaseCommand):
    help = "Reclaim storage for release artifacts older than the three latest releases."

    def handle(self, *args, **options):
        cleanup_release_files()
