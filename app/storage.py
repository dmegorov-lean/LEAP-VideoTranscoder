import asyncio
import os
from datetime import datetime, timedelta, timezone

from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas


def is_configured() -> bool:
    return bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING"))


def make_blob_prefix(job_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{ts}-{job_id}"


def _conn_str() -> str:
    return os.environ["AZURE_STORAGE_CONNECTION_STRING"]


def _container() -> str:
    return os.environ.get("AZURE_STORAGE_CONTAINER", "jobs")


async def upload(local_path: str, blob_name: str) -> None:
    conn_str, container = _conn_str(), _container()

    def _do() -> None:
        client = BlobServiceClient.from_connection_string(conn_str)
        blob = client.get_blob_client(container=container, blob=blob_name)
        with open(local_path, "rb") as f:
            blob.upload_blob(f, overwrite=True)

    await asyncio.to_thread(_do)


def sas_url(blob_name: str, expires_hours: int = 1) -> str:
    client = BlobServiceClient.from_connection_string(_conn_str())
    sas_token = generate_blob_sas(
        account_name=client.account_name,
        container_name=_container(),
        blob_name=blob_name,
        account_key=client.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=expires_hours),
    )
    return (
        f"https://{client.account_name}.blob.core.windows.net"
        f"/{_container()}/{blob_name}?{sas_token}"
    )
