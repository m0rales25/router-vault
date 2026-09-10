import boto3
from botocore.client import Config
from app.config import settings

def s3():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4"),
    )

def ensure_bucket():
    client = s3()
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except Exception:
        client.create_bucket(Bucket=settings.s3_bucket)

def upload_file(local_path: str, object_key: str):
    s3().upload_file(local_path, settings.s3_bucket, object_key)

def presigned_download(object_key: str, expires=600):
    return s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": object_key},
        ExpiresIn=expires,
    )


def delete_object(object_key: str):
    s3().delete_object(
        Bucket=settings.s3_bucket,
        Key=object_key,
    )


def get_object(object_key: str):
    return s3().get_object(
        Bucket=settings.s3_bucket,
        Key=object_key,
    )
