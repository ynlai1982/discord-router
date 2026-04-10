#!/usr/bin/env bash
set -u

# Limitation: /react, /edit_message, and /download_attachment happy paths need a real
# bot-authored message_id or attachment-bearing message_id. Phase 1 review does not
# assume those exist, so this script exercises negative paths for those endpoints.

ENV_FILE="$HOME/.claude/channels/discord/.env"
BASE_URL="http://127.0.0.1:9876"
MAIN_CHANNEL_ID="${TEST_CHANNEL_ID:?Set TEST_CHANNEL_ID env var to a real allowlisted channel ID}"
FAKE_CHANNEL_ID="999999999999999999"
INVALID_MESSAGE_ID="1"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "FAIL env file not found: $ENV_FILE"
  exit 1
fi

TOKEN_LINE="$(grep -E '^DISCORD_ROUTER_TOKEN=' "$ENV_FILE" | tail -n 1)"
if [[ -z "$TOKEN_LINE" ]]; then
  echo "FAIL DISCORD_ROUTER_TOKEN missing from $ENV_FILE"
  exit 1
fi

DISCORD_ROUTER_TOKEN="${TOKEN_LINE#DISCORD_ROUTER_TOKEN=}"
DISCORD_ROUTER_TOKEN="${DISCORD_ROUTER_TOKEN%\"}"
DISCORD_ROUTER_TOKEN="${DISCORD_ROUTER_TOKEN#\"}"
DISCORD_ROUTER_TOKEN="${DISCORD_ROUTER_TOKEN%\'}"
DISCORD_ROUTER_TOKEN="${DISCORD_ROUTER_TOKEN#\'}"

if [[ -z "$DISCORD_ROUTER_TOKEN" ]]; then
  echo "FAIL DISCORD_ROUTER_TOKEN resolved empty from $ENV_FILE"
  exit 1
fi

HAS_JQ=0
if command -v jq >/dev/null 2>&1; then
  HAS_JQ=1
fi

PASS_COUNT=0
FAIL_COUNT=0

curl_case() {
  local label="$1"
  local expected_http="$2"
  local expect_ok="$3"
  local expect_contains="$4"
  local endpoint="$5"
  local token="$6"
  local payload="$7"

  local tmp_body
  tmp_body="$(mktemp)"
  local http_code
  http_code="$(curl -sS -o "$tmp_body" -w "%{http_code}" \
    -X POST "$BASE_URL$endpoint" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    --data "$payload")"
  local body
  body="$(cat "$tmp_body")"
  rm -f "$tmp_body"

  local ok_match=1
  local contains_match=1
  if [[ "$HAS_JQ" -eq 1 ]]; then
    local ok_value
    # `.ok // empty` collapses both null AND false to empty (jq quirk),
    # so use `tostring` to get a literal "true"/"false" string.
    ok_value="$(printf '%s' "$body" | jq -r 'if has("ok") then .ok | tostring else empty end' 2>/dev/null)"
    if [[ "$expect_ok" != "_" && "$ok_value" != "$expect_ok" ]]; then
      ok_match=0
    fi
  else
    if [[ "$expect_ok" == "true" ]] && ! printf '%s' "$body" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
      ok_match=0
    fi
    if [[ "$expect_ok" == "false" ]] && ! printf '%s' "$body" | grep -q '"ok"[[:space:]]*:[[:space:]]*false'; then
      ok_match=0
    fi
  fi

  if [[ "$expect_contains" != "_" ]] && ! printf '%s' "$body" | grep -Fq "$expect_contains"; then
    contains_match=0
  fi

  if [[ "$http_code" == "$expected_http" && "$ok_match" -eq 1 && "$contains_match" -eq 1 ]]; then
    PASS_COUNT=$((PASS_COUNT + 1))
    echo "PASS $label"
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "FAIL $label"
    echo "  expected http=$expected_http ok=$expect_ok contains=$expect_contains"
    echo "  actual   http=$http_code body=$body"
  fi
}

curl_case "healthz happy" "200" "true" "\"router_pid\"" "/healthz" "$DISCORD_ROUTER_TOKEN" "{}"
curl_case "healthz wrong token" "401" "false" "unauthorized" "/healthz" "wrong-token" "{}"

curl_case "reply happy" "200" "true" "\"message_ids\"" "/reply" "$DISCORD_ROUTER_TOKEN" \
  "{\"chat_id\":\"$MAIN_CHANNEL_ID\",\"text\":\"phase1-test-DELETE-ME\",\"files\":[]}"
curl_case "reply channel rejection" "200" "false" "channel $FAKE_CHANNEL_ID not allowlisted" "/reply" "$DISCORD_ROUTER_TOKEN" \
  "{\"chat_id\":\"$FAKE_CHANNEL_ID\",\"text\":\"phase1-negative\",\"files\":[]}"
curl_case "reply missing file" "200" "false" "file not found" "/reply" "$DISCORD_ROUTER_TOKEN" \
  "{\"chat_id\":\"$MAIN_CHANNEL_ID\",\"text\":\"phase1-negative\",\"files\":[\"/tmp/does-not-exist-phase1.txt\"]}"

curl_case "react invalid message" "200" "false" "_" "/react" "$DISCORD_ROUTER_TOKEN" \
  "{\"chat_id\":\"$MAIN_CHANNEL_ID\",\"message_id\":\"$INVALID_MESSAGE_ID\",\"emoji\":\"👍\"}"
curl_case "react channel rejection" "200" "false" "channel $FAKE_CHANNEL_ID not allowlisted" "/react" "$DISCORD_ROUTER_TOKEN" \
  "{\"chat_id\":\"$FAKE_CHANNEL_ID\",\"message_id\":\"$INVALID_MESSAGE_ID\",\"emoji\":\"👍\"}"

curl_case "edit_message invalid message" "200" "false" "_" "/edit_message" "$DISCORD_ROUTER_TOKEN" \
  "{\"chat_id\":\"$MAIN_CHANNEL_ID\",\"message_id\":\"$INVALID_MESSAGE_ID\",\"text\":\"phase1-edit-test\"}"
curl_case "edit_message channel rejection" "200" "false" "channel $FAKE_CHANNEL_ID not allowlisted" "/edit_message" "$DISCORD_ROUTER_TOKEN" \
  "{\"chat_id\":\"$FAKE_CHANNEL_ID\",\"message_id\":\"$INVALID_MESSAGE_ID\",\"text\":\"phase1-edit-test\"}"

curl_case "fetch_messages happy" "200" "true" "\"messages\"" "/fetch_messages" "$DISCORD_ROUTER_TOKEN" \
  "{\"channel\":\"$MAIN_CHANNEL_ID\",\"limit\":2}"
curl_case "fetch_messages channel rejection" "200" "false" "channel $FAKE_CHANNEL_ID not allowlisted" "/fetch_messages" "$DISCORD_ROUTER_TOKEN" \
  "{\"channel\":\"$FAKE_CHANNEL_ID\",\"limit\":2}"

curl_case "download_attachment invalid message" "200" "false" "_" "/download_attachment" "$DISCORD_ROUTER_TOKEN" \
  "{\"chat_id\":\"$MAIN_CHANNEL_ID\",\"message_id\":\"$INVALID_MESSAGE_ID\"}"
curl_case "download_attachment channel rejection" "200" "false" "channel $FAKE_CHANNEL_ID not allowlisted" "/download_attachment" "$DISCORD_ROUTER_TOKEN" \
  "{\"chat_id\":\"$FAKE_CHANNEL_ID\",\"message_id\":\"$INVALID_MESSAGE_ID\"}"

echo
echo "Passed: $PASS_COUNT"
echo "Failed: $FAIL_COUNT"

if [[ "$FAIL_COUNT" -ne 0 ]]; then
  exit 1
fi
