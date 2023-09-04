import logging
from google.cloud import secretmanager
import google_crc32c


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger()


def get_secret(project_id, secret_id, version_id="latest"):
    """
    Access the payload for the given secret version if one exists. The version
    can be a version number as a string (e.g. "5") or an alias (e.g. "latest").
    """
    secret_client = secretmanager.SecretManagerServiceClient()

    # Build the resource name of the secret version.
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"

    # Access the secret version.
    response = secret_client.access_secret_version(request={"name": name})

    # Verify payload checksum.
    crc32c = google_crc32c.Checksum()
    crc32c.update(response.payload.data)
    if response.payload.data_crc32c != int(crc32c.hexdigest(), 16):
        logger.error(f"Data corruption detected when retrieving secret {secret_id}.")
        return "error"
    payload = response.payload.data.decode("UTF-8")
    return f"{payload}"


def getValueByPath(input_dict, path_string):
    """
    Gets data/value from a dictionary using a dotted accessor-string
    http://stackoverflow.com/a/7534478
    path_string can be key.subkey.subkey.subkey
    """
    return_data = input_dict
    for chunk in path_string.split("."):
        return_data = return_data.get(chunk, {})
    return return_data
