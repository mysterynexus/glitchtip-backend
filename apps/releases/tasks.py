from asgiref.sync import sync_to_async
from django.tasks import task


@task
async def cleanup_organization_release_files_task(organization_id: int):
    from .file_cleanup import cleanup_release_files

    await sync_to_async(cleanup_release_files)(organization_id=organization_id)
