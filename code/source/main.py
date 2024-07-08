import logging
import base64
import os
import json
import requests
from slack_bolt import App
from slack_sdk.errors import SlackApiError

from flask import Flask, request
from utils import get_secret, get_channel_messages, get_message_thread
from google.cloud import pubsub_v1
import vertexai
from datetime import datetime, date, timezone, timedelta
import time
from operator import itemgetter
from fastnumbers import try_float


# from vertexai.language_models import TextGenerationModel
from vertexai.generative_models import GenerativeModel, Part, Content


# Flask adapter
from slack_bolt.adapter.flask import SlackRequestHandler


# TODOs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

PROJECT_ID = os.environ.get("PROJECT_ID", "")
# just the ID not the projects/project-id replace if exists
PROJECT_ID = PROJECT_ID.replace("projects/", "")


# we may use the slack_token later for file retrieval
slack_token = get_secret(
    PROJECT_ID, os.environ.get("SLACK_BOT_TOKEN_NAME", "slack_bot_token")
)
# initialize slack
# process_before_response must be True when running on cloud functions, must be false for cloud run
# no ssl check needed, no way to deploy cloud run without it
slack_app = App(
    process_before_response=False,
    ssl_check_enabled=False,
    token=slack_token,
    signing_secret=get_secret(
        PROJECT_ID, os.environ.get("SLACK_SIGNING_SECRET_NAME", "slack_signing_secret")
    ),
)

slack_client = slack_app.client


def slack_markdown(text):
    text = text.replace("**", "*")
    return text


def send_pubsub_message(message):
    # send a slack message to our pubsub topic
    try:
        publisher = pubsub_v1.PublisherClient()
        topic_path = f"projects/{PROJECT_ID}/topics/slack-messages"
        # the message we send
        message_body = json.dumps(message).encode("utf-8")
        logger.debug(
            f"message is {type(message)} and message_body is {type(message_body)}"
        )
        _ = publisher.publish(topic_path, message_body)
    except Exception as e:
        logger.error(f"pubsub message error: {e}")


def handle_slack_message(message):
    logger.debug(f"handle_slack_message received: {message}")
    try:
        # init vertexAI wth gemini
        vertexai.init(project=PROJECT_ID, location="us-central1")
        generation_model = GenerativeModel("gemini-1.5-pro")
        generation_config = {
            "temperature": 1,
            "max_output_tokens": 2048,
            "top_p": 1.0,
            "top_k": 20,
        }

        # if we haven't already started a thread, start one
        if message["entrypoint"] == "greetings" and "thread_ts" not in message:
            # get a welcome from AI

            prompt = "You are a slack bot. The user has summoned you into a slack thread. Generate a friendly welcome message"
            vertext_response = generation_model.generate_content(
                contents=prompt, generation_config=generation_config
            )
            logger.debug(f"vertext response: {vertext_response}")

            slack_result = slack_client.chat_postMessage(
                channel=message["channel"],
                text=f".. <@{message['user']}> {vertext_response.text}",
                mrkdwn=True,
                thread_ts=message["ts"],
            )
            logger.debug(slack_result)
        # is it an existing thread
        if message["entrypoint"] == "thread_reply" and "thread_ts" in message:
            # check if we are in the thread so we don't reply to random threads
            reply_with_ai = False
            slack_identity = slack_client.auth_test()
            SLACK_USER_ID = slack_identity["user_id"]
            # see if this thread involves us
            # search the history based on this thread timestamp
            slack_result = slack_client.conversations_history(
                channel=message["channel"],
                oldest=message["thread_ts"],
                inclusive=True,
                limit=1,
            )
            logger.debug("HERE IS THE THREAD LEAD IN")
            logger.debug(slack_result)

            if (
                slack_result
                and "messages" in slack_result
                and SLACK_USER_ID in slack_result["messages"][0]["reply_users"]
            ):
                logger.debug("WE ARE IN THE THREAD")
                reply_with_ai = True
            else:
                logger.debug("THIS ISN'T A THREAD FOR US")
            if reply_with_ai:
                # get the thread history to send to the ai as context
                thread_history = get_message_thread(
                    slack_client=slack_client,
                    channel_id=message["channel"],
                    thread_ts=message["thread_ts"],
                )

                prompt = f"You are a slack bot in a thread with a person. Please respond kindly to this prompt using slack markdown formatting: \n {message['text']}"
                # vertext_response = generation_model.predict(
                #     prompt=prompt, temperature=1, max_output_tokens=2048
                # )
                # create the ai chat history for gemini
                # ensuring that multiturn requests alternate between user and model.
                # adding 'parts' if multiple messages are from either a user or a model
                # accounting for any files uploaded to the thread

                thread_messages = []
                for thread_message in thread_history:
                    role = "user"
                    if "bot_id" in thread_message:
                        role = "model"
                    # was the last message the same role? Append a new part
                    if (
                        thread_messages
                        and thread_messages[(len(thread_messages) - 1)].role == role
                    ):
                        # append this as another part
                        # the native objects in Content are read only, so we serialize to manipulate
                        d_message = thread_messages[len(thread_messages) - 1].to_dict()
                        if thread_message["text"]:
                            d_message["parts"].append({"text": thread_message["text"]})

                        # files?
                        if "files" in thread_message:
                            for thread_file in thread_message["files"]:
                                r = requests.get(
                                    thread_file["url_private"],
                                    headers={
                                        "Authorization": "Bearer %s" % slack_token
                                    },
                                )
                                file_data = r.content  # get binary content
                                file_part = Part.from_data(
                                    file_data, thread_file["mimetype"]
                                ).to_dict()
                                d_message["parts"].append(file_part)
                        thread_messages[len(thread_messages) - 1] = Content.from_dict(
                            d_message
                        )

                    else:
                        # make a new turn in the chat
                        # we can make and append parts since this is new Content and not read only
                        chat_parts = []
                        # text?
                        if thread_message["text"]:
                            chat_parts.append(Part.from_text(thread_message["text"]))
                        # files?
                        if "files" in thread_message:
                            for thread_file in thread_message["files"]:
                                r = requests.get(
                                    thread_file["url_private"],
                                    headers={
                                        "Authorization": "Bearer %s" % slack_token
                                    },
                                )
                                file_data = r.content  # get binary content
                                chat_parts.append(
                                    Part.from_data(file_data, thread_file["mimetype"])
                                )

                        thread_messages.append(
                            Content(
                                role=role,
                                parts=chat_parts,
                            )
                        )
                logger.debug(thread_messages)

                vertext_response = generation_model.generate_content(
                    contents=thread_messages, generation_config=generation_config
                )
                logger.debug(f"vertext response: {vertext_response}")
                ai_response = vertext_response.text

                if not ai_response:
                    ai_response = "Hrm.. dunno how to respond"

                blocks = [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": slack_markdown(ai_response)},
                    }
                ]
                slack_result = slack_client.chat_postMessage(
                    channel=message["channel"],
                    blocks=blocks,
                    thread_ts=message["ts"],
                )
                logger.debug(slack_result)
            return

        if message["entrypoint"] == "summarize_thread_request":
            # someone in a thread asked for a summary of the thread
            logger.debug(message)
            # we get the message reacted to, not the thread
            # figure out the thread ts
            slack_result = slack_client.conversations_replies(
                channel=message["channel"], ts=message["ts"]
            )
            logger.debug("MESSAGE IN CONTEXT OF THREAD")
            logger.debug(slack_result)
            thread_messages = []
            thread_ts = None
            if "messages" in slack_result:
                # now we get the thread ts
                thread_ts = slack_result["messages"][0]["thread_ts"]
                # get the thread messages
                thread_messages = get_message_thread(
                    slack_client,
                    channel_id=message["channel"],
                    thread_ts=thread_ts,
                )
            # anything to summarize?
            if not len(thread_messages):
                slack_client.chat_postEphemeral(
                    channel=message["channel"],
                    user=message["user"],
                    text="Sorry, doesn't appear to be any messages to summarize",
                )
                return

            sorted_history = sorted(thread_messages, key=itemgetter("ts"))
            # format thread for ai
            channel_conversation = ""
            # cache of user id to name
            slack_users = {}
            for slack_message in sorted_history:
                # resolve user ids if we don't already know them
                slack_user_id = slack_message["user"]
                user_name = ""
                if slack_user_id not in slack_users:
                    user_result = slack_client.users_info(user=slack_message["user"])
                    user_name = user_result["user"]["profile"]["real_name"]
                    slack_users[slack_user_id] = user_name
                else:
                    user_name = slack_users[slack_user_id]
                channel_conversation += f"{user_name}: {slack_message['text']}\n"

            # Prompt the AI
            logger.debug(f"CONVERSATION PROMPT: {channel_conversation}")
            prompt = f"You are a slackbot and have been asked to summarize the following conversation:\n {channel_conversation}"
            vertext_response = generation_model.generate_content(
                contents=prompt, generation_config=generation_config
            )
            logger.debug(f"vertext response: {vertext_response}")
            ai_response = vertext_response.text
            if not ai_response:
                ai_response = "Hrm.. dunno how to respond"

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": slack_markdown(
                            f"Summary of the thread:\n {ai_response}"
                        ),
                    },
                }
            ]

            # return result as an ephemeral message
            slack_result = slack_client.chat_postEphemeral(
                channel=message["channel"],
                user=message["user"],
                thread_ts=thread_ts,
                blocks=blocks,
            )
            return

        if message["entrypoint"] == "summarize_channel_request":
            # we've been asked to summarize a channel
            try:
                slack_result = slack_client.conversations_info(
                    channel=message["channel_id"]
                )
                # are we in the channel?
                if not slack_result.data["channel"]["is_member"]:
                    slack_client.conversations_join(channel=message["channel_id"])
            except Exception as e:
                logger.error("Error accessing channel: {}".format(e))
                # start a DM with the user to tell them we aren't in the channel
                slack_result = slack_client.conversations_open(users=message["user_id"])
                slack_client.chat_postMessage(
                    channel=slack_result["channel"]["id"],
                    text="I don't seem to have access to the channel to summarize for you, sorry!",
                )
                return
            # what's the latest in the channel?
            slack_result = slack_client.conversations_history(
                limit=1, channel=message["channel_id"]
            )
            # anything to summarize?
            if "messages" not in slack_result.data or not len(
                slack_result.data["messages"]
            ):
                slack_client.chat_postEphemeral(
                    channel=message["channel_id"],
                    user=message["user_id"],
                    text="Doesn't appear to be any messages to summarize",
                )
                return
            # get the latest timestamp
            latest_timestamp = slack_result.data["messages"][0]["ts"]
            # set the lookback period
            lookback = 1
            try:
                prompt = f"""
                A person asked you to summarize content for a slack channel. The person invoking you said: 

                'summarize {message['text']} '

                If the person gave you a time frame, what number of hours? 
                If the person did not give a time frame assume 1 hour. 
                Answer with only the number.
                """

                response = generation_model.generate_content(
                    contents=prompt,
                    generation_config={
                        "temperature": 0.1,
                        "max_output_tokens": 8,
                        "top_p": 1.0,
                        "top_k": 40,
                    },
                )
                lookback = try_float(response.text, nan=1, on_fail=1)

            except Exception as e:
                logger.error(f"Error determining lookback period: {e}")

            search_timestamp = datetime.fromtimestamp(
                float(latest_timestamp)
            ) - timedelta(hours=lookback)
            # convert to a timestamp
            search_timestamp = time.mktime(search_timestamp.timetuple())
            # search the channel
            # get full channel history with all threads
            channel_id = message["channel_id"]
            channel_history = get_channel_messages(
                slack_client, channel_id, oldest=search_timestamp
            )
            thread_history = []
            for history_message in channel_history:
                if "thread_ts" in history_message:
                    thread_messages = get_message_thread(
                        slack_client,
                        channel_id=channel_id,
                        thread_ts=history_message["thread_ts"],
                    )
                    thread_history.extend(thread_messages)
            # add the threads
            channel_history.extend(thread_history)
            # sort it all by ts
            sorted_history = sorted(channel_history, key=itemgetter("ts"))
            # add any threads
            channel_conversation = ""
            # cache of user id to name
            slack_users = {}
            for slack_message in sorted_history:
                # resolve user ids if we don't already know them
                slack_user_id = slack_message["user"]
                user_name = ""
                if slack_user_id not in slack_users:
                    user_result = slack_client.users_info(user=slack_message["user"])
                    user_name = user_result["user"]["profile"]["real_name"]
                    slack_users[slack_user_id] = user_name
                else:
                    user_name = slack_users[slack_user_id]
                channel_conversation += f"{user_name}: {slack_message['text']}\n"

            # Prompt the AI
            prompt = f"You are a slackbot and have been asked to summarize the following conversation:\n {channel_conversation}"
            vertext_response = generation_model.generate_content(
                contents=prompt, generation_config=generation_config
            )
            logger.debug(f"vertext response: {vertext_response}")
            ai_response = vertext_response.text
            if not ai_response:
                ai_response = "Hrm.. dunno how to respond"
            lookback_timeperiod = "hour"
            if lookback > 1:
                lookback_timeperiod = "hours"

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": slack_markdown(
                            f"Summary of the last {lookback} {lookback_timeperiod}:\n {ai_response}"
                        ),
                    },
                }
            ]
            slack_result = slack_client.chat_postEphemeral(
                channel=message["channel_id"], user=message["user_id"], blocks=blocks
            )
            return

    except Exception as e:
        logger.error(f"Error posting message: {e}")


def ack_message(ack):
    # lazy listener, ack response
    ack(f"on it")


def greetings(message):
    message["entrypoint"] = "greetings"
    logger.debug(message)
    send_pubsub_message(message)


slack_app.message("hello ai|howdy ai|<!here>|hey ai")(ack=ack_message, lazy=[greetings])


def thread_reply(message):
    # should be subtype of message_replied, but a bug in events api omits it
    # so we check for thread_ts
    logger.debug(f"app.event message received: {message}")
    if "parent_user_id" in message and "thread_ts" in message:
        message["entrypoint"] = "thread_reply"
        logger.debug(message)
        send_pubsub_message(message)


slack_app.event("message")(ack=ack_message, lazy=[thread_reply])


def summarize(ack, command, respond):
    logger.debug(command)
    command["entrypoint"] = "summarize_channel_request"
    send_pubsub_message(command)
    respond("")


slack_app.command("/summarize")(ack=ack_message, lazy=[summarize])


def reaction_handler(event, client, say, context):
    logger.info(event)

    # we trigger on any emoji named summary, summarize, etc
    if "summar" not in event["reaction"]:
        # early exit
        return

    logger.info("summarize emoji request")

    # start with the event
    # type: reaction_added, user: slack userid, reaction: emoji name, item_user, event_ts, item (type, channel, ts)
    message = event
    channel = event["item"]["channel"]
    message["channel"] = channel
    message["ts"] = event["item"]["ts"]
    message["entrypoint"] = "summarize_thread_request"
    # send to pubsub, do the rest async
    send_pubsub_message(message=message)


slack_app.event("reaction_added")(ack=ack_message, lazy=[reaction_handler])

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
    logger.debug(f"{content_type} request received")

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
