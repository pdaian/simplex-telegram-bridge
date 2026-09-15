# SimpleX → Telegram

Forward received text from one SimpleX contact or group to one Telegram chat.
Messages arrive as `Alice: hello`. Python 3.11+; one dependency.

## Setup

1. Install the dependency in your Python environment:

   ```sh
   python3 -m pip install --no-cache-dir --no-compile -r requirements.txt
   ```

2. Install [SimpleX Chat CLI](https://github.com/simplex-chat/simplex-chat/blob/stable/docs/CLI.md).
   Create a profile and connect to the contact or join the group you want to relay.
   Start its [WebSocket API](https://github.com/simplex-chat/simplex-chat/blob/stable/bots/README.md):

   ```sh
   simplex-chat -p 5225
   ```

   Keep this process running on the same machine. Its API has no authentication.

3. Create a Telegram bot with [BotFather](https://t.me/BotFather).
   Add it to the destination group with permission to send messages, or make it a
   channel administrator with permission to post. For a private chat, message the
   bot first. Send a command such as `/start` in the destination, then find its
   numeric `chat.id` in [getUpdates](https://core.telegram.org/bots/api#getupdates)
   (`message.chat.id`, or `channel_post.chat.id` for channels).

## Run

```sh
export SIMPLEX_CHAT_ID='#1'                  # #<groupId> or @<contactId>
export TELEGRAM_CHAT_ID='-1001234567890'
export TELEGRAM_BOT_TOKEN='123456:your-token'
python3 -B bridge.py
```

`SIMPLEX_WS_URL` defaults to `ws://127.0.0.1:5225`. Configuration comes from the
environment; `.env` files are not loaded. Stop with Ctrl-C or SIGTERM.

### Find the SimpleX chat ID

Connect an interactive client before starting the bridge:

```sh
python3 -B -m websockets ws://127.0.0.1:5225
```

Send `{"corrId":"user","cmd":"/user"}` and read `resp.user.userId`.
Use that ID in `{"corrId":"groups","cmd":"/_groups 1"}` or
`{"corrId":"contacts","cmd":"/_contacts 1"}`. Look for the matching
`groupId` or `contactId` and prefix it with `#` or `@`. Close the client with Ctrl-D.
These are database IDs, not display names.

## Behavior

- Uses SimpleX's `newChatItems` events. Only incoming text and link text are relayed.
- Ignores attachments, edits, deletions, sent messages, and group support chats.
- Splits long messages for Telegram, sends plain text, and disables link previews.
- Reconnects to SimpleX and retries Telegram network errors, rate limits, and server
  errors. Other Telegram errors stop the process with a nonzero exit code.
- Stores no persistent state. Messages missed while disconnected are not replayed. An uncertain
  Telegram send can be duplicated on retry; stopping can lose pending messages.
- Forwarded messages leave SimpleX's encrypted conversation and are visible to
  Telegram and the destination chat's members.

## Check

```sh
python3 -B -m unittest discover -s tests -v
```
