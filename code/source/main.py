import logging
import base64
import os
import json
from slack_bolt import App
from flask import Flask, request
from utils import get_secret, getValueByPath
from google.cloud import pubsub_v1
import vertexai
from vertexai.language_models import (
    TextGenerationModel,
    TextEmbeddingModel,
    ChatModel,
    InputOutputTextPair,
)


# Flask adapter
from slack_bolt.adapter.flask import SlackRequestHandler


# TODOs
# - Add AI

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger()

PROJECT_ID = os.environ.get("PROJECT_ID", "")
# just the ID not the projects/project-id replace if exists
PROJECT_ID = PROJECT_ID.replace("projects/", "")

# init vertexAI
vertexai.init(project=PROJECT_ID, location="us-central1")
generation_model = TextGenerationModel.from_pretrained("text-bison@001")

# initialize slack
# process_before_response must be True when running on cloud functions, must be false for cloud run
# no ssl check needed, no way to deploy cloud run without it
slack_app = App(
    process_before_response=False,
    ssl_check_enabled=False,
    token=get_secret(
        PROJECT_ID, os.environ.get("SLACK_BOT_TOKEN_NAME", "slack_bot_token")
    ),
    signing_secret=get_secret(
        PROJECT_ID, os.environ.get("SLACK_SIGNING_SECRET_NAME", "slack_signing_secret")
    ),
)

slack_client = None


def send_pubsub_message(message):
    # send a slack message to our pubsub topic
    try:
        publisher = pubsub_v1.PublisherClient()
        topic_path = f"projects/{PROJECT_ID}/topics/slack-messages"
        # the message we send
        message_body = json.dumps(message).encode("utf-8")
        logger.info(
            f"message is {type(message)} and message_body is {type(message_body)}"
        )
        _ = publisher.publish(topic_path, message_body)
    except Exception as e:
        logger.error(f"pubsub message error: {e}")


def handle_slack_message(message):
    logger.debug(f"handle_slack_message received: {message}")
    try:
        # if we haven't already started a thread, start one
        if message["entrypoint"] == "greetings" and "thread_ts" not in message:
            # get a welcome from AI

            prompt = "Generate a friendly welcome message"
            vertext_response = generation_model.predict(prompt=prompt)

            slack_result = slack_client.chat_postMessage(
                channel=message["channel"],
                text=f".. <@{message['user']}> {vertext_response.text}",
                thread_ts=message["ts"],
            )
            logger.debug(slack_result)
        if message["entrypoint"] == "thread_reply" and "thread_ts" in message:
            prompt = message["text"]
            vertext_response = generation_model.predict(prompt=prompt)
            slack_result = slack_client.chat_postMessage(
                channel=message["channel"],
                text=vertext_response.text,
                thread_ts=message["ts"],
            )
            logger.debug(slack_result)
    except Exception as e:
        logger.error(f"Error posting message: {e}")


def ack_message(ack):
    # lazy listener, ack response
    ack(f"on it")


@slack_app.message("hello ai|howdy ai|<!here>|hey ai")
def greetings(ack, client, say, message):
    global slack_client
    slack_client = client
    ack()
    message["entrypoint"] = "greetings"
    logger.debug(message)
    send_pubsub_message(message)


@slack_app.event("message")
def thread_reply(ack, client, message):
    # should be subtype of message_replied, but a bug in events api omits it
    # so we check for thread_ts
    logger.debug(f"app.event message received: {message}")
    ack()
    if "parent_user_id" in message and "thread_ts" in message:
        global slack_client
        slack_client = client
        message["entrypoint"] = "thread_reply"
        logger.debug(message)
        send_pubsub_message(message)


flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def hello_world():
    # a simple hello to help debug cloud run url access
    name = os.environ.get("NAME", "World")
    return "HELLO {}!".format(name)


# slack event entry point
slack_handler = SlackRequestHandler(slack_app)


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return slack_handler.handle(request)


# pubsub callbacks
# using pubsub to be async from slack's strict X secs to response rule
# we ack every request within the limit and queue it in pubsub for the actual response


@flask_app.route("/", methods=["POST"])
def default_post_route():
    envelope = {}
    content_type = request.headers.get("Content-Type")
    headers = dict(request.headers)
    headers.pop("Authorization", None)  # avoid logging secrets
    logger.debug(f"headers: {headers}")
    logger.info(f"{content_type} request received")

    if content_type == "application/json":
        envelope = request.json
    else:
        envelope = json.loads(request.data)
    logger.debug(f"post envelope is {envelope}")

    # pubsub message?
    if "message" in envelope and "subscription" in envelope:
        pubsub_message = envelope["message"]

        if isinstance(pubsub_message, dict) and "data" in pubsub_message:
            message_body_string = (
                base64.b64decode(pubsub_message["data"]).decode("utf-8").strip()
            )
            logger.debug(f"pubsub message is: {message_body_string}")
            try:
                message_dict = json.loads(message_body_string)
                handle_slack_message(message_dict)
            except Exception as e:
                logger.error(f"handle_slack_message call error: {e}")
                pass

    # let pubsub know we are done with this message
    return ("", 204)
