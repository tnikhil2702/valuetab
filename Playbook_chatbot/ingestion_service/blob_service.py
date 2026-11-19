from azure.storage.blob.aio import BlobServiceClient
from Playbook_chatbot.shared.config import *
from azure.storage.blob import generate_blob_sas, BlobSasPermissions
from datetime import datetime, timedelta

blob_service = BlobServiceClient.from_connection_string(
    AZURE_BLOB_CONNECTION_STRING
)

async def upload_blob(file_bytes: bytes, filename: str):

    container = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
    blob_client = container.get_blob_client(filename)

    await blob_client.upload_blob(file_bytes, overwrite=True, timeout=2000, connection_timeout=1800, max_concurrency=2)

async def blob_exists(filename: str) -> bool:

    container = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
    blob_client = container.get_blob_client(filename)

    return await blob_client.exists()


async def delete_blob(filename: str):

    container = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
    blob_client = container.get_blob_client(filename)

    if await blob_client.exists():
        await blob_client.delete_blob()


async def list_blobs():

    container = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
    files = []
    async for blob in container.list_blobs():
        files.append({
            "filename": blob.name,
            "size_kb": round(blob.size / 1024, 2) if blob.size else 0,
            "last_modified": blob.last_modified.isoformat() if blob.last_modified else None
        })
    return files

async def generate_blob_sas_url(filename: str, expiry_minutes: int = 60) -> str:
    """
    Generate SAS download URL for a blob
    """
    sas_token = generate_blob_sas(
        account_name=AZURE_BLOB_ACCOUNT_NAME,
        container_name=AZURE_BLOB_CONTAINER,
        blob_name=filename,
        account_key=AZURE_BLOB_ACCOUNT_KEY,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(minutes=expiry_minutes),
    )

    return f"https://{AZURE_BLOB_ACCOUNT_NAME}.blob.core.windows.net/{AZURE_BLOB_CONTAINER}/{filename}?{sas_token}"