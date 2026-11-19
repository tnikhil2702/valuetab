from azure.storage.blob.aio import BlobServiceClient
from FAA_MedCert_Case_Builder.shared.config import *

blob_service = BlobServiceClient.from_connection_string(
    AZURE_BLOB_CONNECTION_STRING
)

async def upload_blob(file, filename: str):

    container = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
    blob_client = container.get_blob_client(filename)

    await blob_client.upload_blob(file, overwrite=True, timeout=2000, connection_timeout=1800, max_concurrency=2)

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

async def download_blob(filename: str) -> bytes:
    container = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
    blob_client = container.get_blob_client(filename)

    stream = await blob_client.download_blob()
    return await stream.readall()