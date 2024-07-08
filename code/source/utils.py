import logging
from google.cloud import secretmanager
import google_crc32c
from slack_sdk.errors import SlackApiError


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


def get_channel_messages(slack_client, channel_id, message_count=0, oldest=1):
    # return conversation history
    conversation_history = []

    try:
        # Call the conversations.history method using the WebClient
        # conversations.history returns the first 100 messages by default
        # paginated, get the first page
        result = slack_client.conversations_history(
            channel=channel_id, limit=message_count, oldest=oldest
        )
        conversation_history = result["messages"] if "messages" in result else []
        while result.data["has_more"]:

            result = slack_client.conversations_history(
                channel=channel_id,
                limit=message_count,
                cursor=result["response_metadata"]["next_cursor"],
                latest=result["messages"][1]["ts"],
            )
            conversation_history.extend(result["messages"])

        # results
        logger.debug(
            "{} messages found in {}".format(len(conversation_history), channel_id)
        )
        return conversation_history

    except SlackApiError as e:
        logger.error("Error accessing history: {}".format(e))


def get_message_thread(slack_client, channel_id, thread_ts):
    thread_history = []
    try:
        # Call the conversations.replies method using the WebClient
        # conversations.history returns the first 100 messages by default
        # paginated, get the first page
        result = slack_client.conversations_replies(channel=channel_id, ts=thread_ts)
        thread_history = result["messages"] if "messages" in result else []
        while result.data["has_more"]:

            result = slack_client.conversations_replies(
                channel=channel_id,
                ts=thread_ts,
                cursor=result["response_metadata"]["next_cursor"],
                latest=result["messages"][1]["ts"],
            )
            thread_history.extend(result["messages"])

        # results
        logger.debug("{} messages found in {}".format(len(thread_history), thread_ts))
        return thread_history

    except SlackApiError as e:
        logger.error("Error accessing history: {}".format(e))
