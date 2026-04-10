# Phase 0 Spec: Discord MCP Fork Research

## 1. File overview

- File: `~/.claude/plugins/cache/claude-plugins-official/discord/0.0.4/server.ts`
- Total line count: `893`
- Helper files imported from inside `0.0.4`: none. All imports are package or stdlib imports.

### Import statements

#### (a) Discord SDK imports

```ts
import {
  Client,
  GatewayIntentBits,
  Partials,
  ChannelType,
  ButtonBuilder,
  ButtonStyle,
  ActionRowBuilder,
  type Message,
  type Attachment,
  type Interaction,
} from 'discord.js'
```

#### (b) MCP SDK imports

```ts
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
```

#### (c) Node/bun stdlib

```ts
import { randomBytes } from 'crypto'
import { readFileSync, writeFileSync, mkdirSync, readdirSync, rmSync, statSync, renameSync, realpathSync, chmodSync } from 'fs'
import { homedir } from 'os'
import { join, sep } from 'path'
```

#### (d) Other

```ts
import { z } from 'zod'
```

### Top-level constants and configuration loading

- `STATE_DIR` (line 37): `process.env.DISCORD_STATE_DIR ?? join(homedir(), '.claude', 'channels', 'discord')`
- `ACCESS_FILE` (line 38): `join(STATE_DIR, 'access.json')`
- `APPROVED_DIR` (line 39): `join(STATE_DIR, 'approved')`
- `ENV_FILE` (line 40): `join(STATE_DIR, '.env')`
- `.env` loader (lines 42-51):
  - Calls `chmodSync(ENV_FILE, 0o600)`
  - Reads `ENV_FILE`
  - Parses `KEY=value`
  - Only sets `process.env[key]` when not already defined
- `TOKEN` (line 53): `process.env.DISCORD_BOT_TOKEN`
- `STATIC` (line 54): `process.env.DISCORD_ACCESS_MODE === 'static'`
- Hard failure if `DISCORD_BOT_TOKEN` missing (lines 56-63)
- `INBOX_DIR` (line 64): `join(STATE_DIR, 'inbox')`
- Unhandled error logging (lines 66-73):
  - `process.on('unhandledRejection', ...)`
  - `process.on('uncaughtException', ...)`
- `PERMISSION_REPLY_RE` (line 79): `/^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$/i`
- Discord client construction (lines 81-90):
  - `new Client(...)`
  - intents:
    - `GatewayIntentBits.DirectMessages`
    - `GatewayIntentBits.Guilds`
    - `GatewayIntentBits.GuildMessages`
    - `GatewayIntentBits.MessageContent`
  - partials:
    - `Partials.Channel`
- Access/config constants:
  - `MAX_CHUNK_LIMIT` (line 132): `2000`
  - `MAX_ATTACHMENT_BYTES` (line 133): `25 * 1024 * 1024`
- Access file lifecycle:
  - `defaultAccess()` lines 123-130
  - `readAccessFile()` lines 151-172
  - `BOOT_ACCESS` static-mode snapshot lines 177-189
  - `loadAccess()` lines 191-193
  - `saveAccess()` lines 195-201
  - `pruneExpired()` lines 203-213
- Other top-level state:
  - `recentSentIds` lines 222-232
  - `pendingPermissions` line 467

## 2. Tool catalog (5 tools)

### Tool: `reply`

- Tool name: `reply`
- Description string:

```ts
'Reply on Discord. Pass chat_id from the inbound message. Optionally pass reply_to (message_id) for threading, and files (absolute paths) to attach images or other files.'
```

- Input schema:

```ts
{
  type: 'object',
  properties: {
    chat_id: { type: 'string' },
    text: { type: 'string' },
    reply_to: {
      type: 'string',
      description: 'Message ID to thread under. Use message_id from the inbound <channel> block, or an id from fetch_messages.',
    },
    files: {
      type: 'array',
      items: { type: 'string' },
      description: 'Absolute file paths to attach (images, logs, etc). Max 10 files, 25MB each.',
    },
  },
  required: ['chat_id', 'text'],
}
```

- Implementation summary:
  - Calls `fetchAllowedChannel(chat_id)` and rejects non-sendable channels.
  - Validates each file with `assertSendable`, `statSync`, 25MB per-file cap, and 10-file max.
  - Loads access config for `textChunkLimit`, `chunkMode`, and `replyToMode`, then splits long text with `chunk(...)`.
  - Sends one or more Discord messages via `ch.send(...)`, attaching files only on the first chunk and optionally using reply threading.
  - Tracks sent IDs with `noteSent(sent.id)` and returns either a single ID or a multi-part summary; wraps partial-send failures with chunk counts.
- Return value format on success:

```ts
{ content: [{ type: 'text', text: 'sent (id: ...)' }] }
```

or

```ts
{ content: [{ type: 'text', text: 'sent N parts (ids: ...)' }] }
```

- Error format:

```ts
{
  content: [{ type: 'text', text: `reply failed: ${msg}` }],
  isError: true,
}
```

- Source line range: schema `520-540`; implementation `602-653`

### Tool: `react`

- Tool name: `react`
- Description string:

```ts
'Add an emoji reaction to a Discord message. Unicode emoji work directly; custom emoji need the <:name:id> form.'
```

- Input schema:

```ts
{
  type: 'object',
  properties: {
    chat_id: { type: 'string' },
    message_id: { type: 'string' },
    emoji: { type: 'string' },
  },
  required: ['chat_id', 'message_id', 'emoji'],
}
```

- Implementation summary:
  - Calls `fetchAllowedChannel(chat_id)`.
  - Fetches the target message with `ch.messages.fetch(message_id)`.
  - Adds the reaction with `msg.react(emoji)`.
  - Returns fixed text `reacted`.
  - Relies on the outer `catch` for all errors.
- Return value format on success:

```ts
{ content: [{ type: 'text', text: 'reacted' }] }
```

- Error format:

```ts
{
  content: [{ type: 'text', text: `react failed: ${msg}` }],
  isError: true,
}
```

- Source line range: schema `541-553`; implementation `677-682`

### Tool: `edit_message`

- Tool name: `edit_message`
- Description string:

```ts
'Edit a message the bot previously sent. Useful for interim progress updates. Edits don\'t trigger push notifications — send a new reply when a long task completes so the user\'s device pings.'
```

- Input schema:

```ts
{
  type: 'object',
  properties: {
    chat_id: { type: 'string' },
    message_id: { type: 'string' },
    text: { type: 'string' },
  },
  required: ['chat_id', 'message_id', 'text'],
}
```

- Implementation summary:
  - Calls `fetchAllowedChannel(chat_id)`.
  - Fetches the target message with `ch.messages.fetch(message_id)`.
  - Calls `msg.edit(text)`.
  - Returns the edited message ID from `edited.id`.
  - Relies on the outer `catch` for all errors.
- Return value format on success:

```ts
{ content: [{ type: 'text', text: `edited (id: ${edited.id})` }] }
```

- Error format:

```ts
{
  content: [{ type: 'text', text: `edit_message failed: ${msg}` }],
  isError: true,
}
```

- Source line range: schema `554-566`; implementation `683-688`

### Tool: `download_attachment`

- Tool name: `download_attachment`
- Description string:

```ts
'Download attachments from a specific Discord message to the local inbox. Use after fetch_messages shows a message has attachments (marked with +Natt). Returns file paths ready to Read.'
```

- Input schema:

```ts
{
  type: 'object',
  properties: {
    chat_id: { type: 'string' },
    message_id: { type: 'string' },
  },
  required: ['chat_id', 'message_id'],
}
```

- Implementation summary:
  - Calls `fetchAllowedChannel(chat_id)`.
  - Fetches the target message with `ch.messages.fetch(message_id)`.
  - If there are no attachments, returns `message has no attachments`.
  - For each attachment, calls helper `downloadAttachment(att)`, then formats path, safe filename, content type, and KB size.
  - Returns a newline-joined text block listing downloaded file paths.
- Return value format on success:

```ts
{ content: [{ type: 'text', text: 'message has no attachments' }] }
```

or

```ts
{
  content: [{
    type: 'text',
    text: `downloaded ${lines.length} attachment(s):\n${lines.join('\n')}`,
  }],
}
```

- Error format:

```ts
{
  content: [{ type: 'text', text: `download_attachment failed: ${msg}` }],
  isError: true,
}
```

- Source line range: schema `567-578`; implementation `689-704`

### Tool: `fetch_messages`

- Tool name: `fetch_messages`
- Description string:

```ts
"Fetch recent messages from a Discord channel. Returns oldest-first with message IDs. Discord's search API isn't exposed to bots, so this is the only way to look back."
```

- Input schema:

```ts
{
  type: 'object',
  properties: {
    channel: { type: 'string' },
    limit: {
      type: 'number',
      description: 'Max messages (default 20, Discord caps at 100).',
    },
  },
  required: ['channel'],
}
```

- Implementation summary:
  - Calls `fetchAllowedChannel(channel)`.
  - Clamps `limit` to `100`, default `20`.
  - Fetches history with `ch.messages.fetch({ limit })`.
  - Uses `client.user?.id` to label bot-authored messages as `me`.
  - Reverses Discord’s returned collection to oldest-first and formats each row as one line, sanitizing embedded newlines in message content.
- Return value format on success:

```ts
{ content: [{ type: 'text', text: '(no messages)' }] }
```

or

```ts
{ content: [{ type: 'text', text: out }] }
```

where `out` is a newline-joined string of rows like:

```ts
`[${m.createdAt.toISOString()}] ${who}: ${text}  (id: ${m.id}${atts})`
```

- Error format:

```ts
{
  content: [{ type: 'text', text: `fetch_messages failed: ${msg}` }],
  isError: true,
}
```

- Source line range: schema `579-594`; implementation `654-676`

### Shared default / unknown-tool behavior

- Unknown tool branch (lines 705-710):

```ts
{
  content: [{ type: 'text', text: `unknown tool: ${req.params.name}` }],
  isError: true,
}
```

- Shared error wrapper for all tool handlers (lines 711-716):

```ts
{
  content: [{ type: 'text', text: `${req.params.name} failed: ${msg}` }],
  isError: true,
}
```

## 3. Discord SDK touchpoint inventory

Exhaustive inventory of Discord SDK use or Discord object access that must be removed, replaced, or accounted for.

- `20-31`

```ts
import {
  Client,
  GatewayIntentBits,
  Partials,
  ChannelType,
  ButtonBuilder,
  ButtonStyle,
  ActionRowBuilder,
  type Message,
  type Attachment,
  type Interaction,
} from 'discord.js'
```

Category: `CONSTRUCTION`
Note: Remove Discord SDK import block; replace with router HTTP client types plus local request/response types.

- `81-90`

```ts
const client = new Client({
  intents: [
    GatewayIntentBits.DirectMessages,
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
  ],
  partials: [Partials.Channel],
})
```

Category: `CONSTRUCTION`
Note: Delete entirely; fork should not create a Discord client.

- `234-291`

```ts
async function gate(msg: Message): Promise<GateResult> {
```

Category: `EVENT_HANDLER`
Note: Entire inbound Discord message gate path disappears unless the router forwards already-gated inbound messages into MCP.

- `241`

```ts
const senderId = msg.author.id
```

Category: `EVENT_HANDLER`
Note: Router would derive sender identity from its own Discord event handling.

- `242`

```ts
const isDM = msg.channel.type === ChannelType.DM
```

Category: `EVENT_HANDLER`
Note: Router would classify DM vs guild/thread before forwarding.

- `265`

```ts
chatId: msg.channelId,
```

Category: `EVENT_HANDLER`
Note: Router would write pending pairing state using its own inbound message metadata.

- `278-280`

```ts
const channelId = msg.channel.isThread()
  ? msg.channel.parentId ?? msg.channelId
  : msg.channelId
```

Category: `EVENT_HANDLER`
Note: Router would resolve thread-parent allowlist logic.

- `294-316`

```ts
async function isMentioned(msg: Message, extraPatterns?: string[]): Promise<boolean> {
```

Category: `EVENT_HANDLER`
Note: Entire mention-detection helper belongs in router if inbound event processing stays there.

- `295`

```ts
if (client.user && msg.mentions.has(client.user)) return true
```

Category: `EVENT_HANDLER`
Note: Router checks direct mention against its bot user.

- `298`

```ts
const refId = msg.reference?.messageId
```

Category: `EVENT_HANDLER`
Note: Router inspects message reference metadata.

- `304-305`

```ts
const ref = await msg.fetchReference()
if (ref.author.id === client.user?.id) return true
```

Category: `EVENT_HANDLER`
Note: Router may need to fetch referenced message via Discord SDK to infer implicit mention.

- `309`

```ts
const text = msg.content
```

Category: `EVENT_HANDLER`
Note: Router extracts inbound text before forwarding.

- `325-363`

```ts
function checkApprovals(): void {
```

Category: `EVENT_HANDLER`
Note: File polling itself is non-Discord, but its outbound DM send path depends on Discord and would move to router if pairing confirmations still send to Discord.

- `351-354`

```ts
const ch = await fetchTextChannel(dmChannelId)
if ('send' in ch) {
  await ch.send("Paired! Say hi to Claude.")
}
```

Category: `TOOL_CALL`
Note: Replace with router-side DM send, likely same internal primitive as reply.

- `392-398`

```ts
async function fetchTextChannel(id: string) {
  const ch = await client.channels.fetch(id)
  if (!ch || !ch.isTextBased()) {
    throw new Error(`channel ${id} not found or not text-based`)
  }
  return ch
}
```

Category: `TOOL_CALL`
Note: Replace with HTTP request to router for channel resolution or let router validate channel ID inside each endpoint.

- `403-413`

```ts
async function fetchAllowedChannel(id: string) {
  const ch = await fetchTextChannel(id)
  const access = loadAccess()
  if (ch.type === ChannelType.DM) {
    if (access.allowFrom.includes(ch.recipientId)) return ch
  } else {
    const key = ch.isThread() ? ch.parentId ?? ch.id : ch.id
    if (key in access.groups) return ch
  }
  throw new Error(`channel ${id} is not allowlisted — add via /discord:access`)
}
```

Category: `TOOL_CALL`
Note: Keep access checks conceptually, but router should enforce them; MCP server can proxy and trust router or keep a duplicated precheck only if access.json remains local.

- `415-428`

```ts
async function downloadAttachment(att: Attachment): Promise<string> {
  if (att.size > MAX_ATTACHMENT_BYTES) {
    throw new Error(`attachment too large: ${(att.size / 1024 / 1024).toFixed(1)}MB, max ${MAX_ATTACHMENT_BYTES / 1024 / 1024}MB`)
  }
  const res = await fetch(att.url)
  const buf = Buffer.from(await res.arrayBuffer())
  const name = att.name ?? `${att.id}`
  const rawExt = name.includes('.') ? name.slice(name.lastIndexOf('.') + 1) : 'bin'
  const ext = rawExt.replace(/[^a-zA-Z0-9]/g, '') || 'bin'
  const path = join(INBOX_DIR, `${Date.now()}-${att.id}.${ext}`)
  mkdirSync(INBOX_DIR, { recursive: true })
  writeFileSync(path, buf)
  return path
}
```

Category: `TOOL_CALL`
Note: Attachment object comes from Discord SDK. In fork, router should probably download/write locally and return paths; otherwise MCP would need attachment URLs from router.

- `433-434`

```ts
function safeAttName(att: Attachment): string {
  return (att.name ?? att.id).replace(/[\[\]\r\n;]/g, '_')
}
```

Category: `TOOL_CALL`
Note: Keep helper logic, but change input type from Discord `Attachment` to router attachment payload shape.

- `473-515`

```ts
mcp.setNotificationHandler(
```

Category: `EVENT_HANDLER`
Note: Handler registration stays MCP-side, but its current implementation sends Discord DMs directly and uses Discord button components.

- `488-503`

```ts
const row = new ActionRowBuilder<ButtonBuilder>().addComponents(
  new ButtonBuilder()
    .setCustomId(`perm:more:${request_id}`)
    .setLabel('See more')
    .setStyle(ButtonStyle.Secondary),
  new ButtonBuilder()
    .setCustomId(`perm:allow:${request_id}`)
    .setLabel('Allow')
    .setEmoji('✅')
    .setStyle(ButtonStyle.Success),
  new ButtonBuilder()
    .setCustomId(`perm:deny:${request_id}`)
    .setLabel('Deny')
    .setEmoji('❌')
    .setStyle(ButtonStyle.Danger),
)
```

Category: `EVENT_HANDLER`
Note: Replace with router-mediated permission prompt delivery, or drop button UI and rely on text reply flow only.

- `507-508`

```ts
const user = await client.users.fetch(userId)
await user.send({ content: text, components: [row] })
```

Category: `EVENT_HANDLER`
Note: Replace with router-side DM send endpoint capable of components, or remove feature.

- `608-609`

```ts
const ch = await fetchAllowedChannel(chat_id)
if (!('send' in ch)) throw new Error('channel is not sendable')
```

Category: `TOOL_CALL`
Note: Replace with `POST /reply` to router.

- `633-639`

```ts
const sent = await ch.send({
  content: chunks[i],
  ...(i === 0 && files.length > 0 ? { files } : {}),
  ...(shouldReplyTo
    ? { reply: { messageReference: reply_to, failIfNotExists: false } }
    : {}),
})
```

Category: `TOOL_CALL`
Note: Replace with router send call carrying `chat_id`, `text`, optional `reply_to`, optional `files`.

- `640`

```ts
noteSent(sent.id)
```

Category: `TOOL_CALL`
Note: If inbound handling leaves the MCP process, this sent-message cache likely moves to router too.

- `641`

```ts
sentIds.push(sent.id)
```

Category: `TOOL_CALL`
Note: Router response must include sent message IDs.

- `655-658`

```ts
const ch = await fetchAllowedChannel(args.channel as string)
const limit = Math.min((args.limit as number) ?? 20, 100)
const msgs = await ch.messages.fetch({ limit })
const me = client.user?.id
```

Category: `TOOL_CALL`
Note: Replace with `POST /fetch_messages`; router can apply limit clamp and bot-self labeling context.

- `664-672`

```ts
.map(m => {
  const who = m.author.id === me ? 'me' : m.author.username
  const atts = m.attachments.size > 0 ? ` +${m.attachments.size}att` : ''
  const text = m.content.replace(/[\r\n]+/g, ' ⏎ ')
  return `[${m.createdAt.toISOString()}] ${who}: ${text}  (id: ${m.id}${atts})`
})
```

Category: `TOOL_CALL`
Note: Formatting can stay MCP-side only if router returns full message metadata; otherwise router can return preformatted rows.

- `678-680`

```ts
const ch = await fetchAllowedChannel(args.chat_id as string)
const msg = await ch.messages.fetch(args.message_id as string)
await msg.react(args.emoji as string)
```

Category: `TOOL_CALL`
Note: Replace with `POST /react`.

- `684-687`

```ts
const ch = await fetchAllowedChannel(args.chat_id as string)
const msg = await ch.messages.fetch(args.message_id as string)
const edited = await msg.edit(args.text as string)
return { content: [{ type: 'text', text: `edited (id: ${edited.id})` }] }
```

Category: `TOOL_CALL`
Note: Replace with `POST /edit_message`.

- `690-699`

```ts
const ch = await fetchAllowedChannel(args.chat_id as string)
const msg = await ch.messages.fetch(args.message_id as string)
if (msg.attachments.size === 0) {
  return { content: [{ type: 'text', text: 'message has no attachments' }] }
}
const lines: string[] = []
for (const att of msg.attachments.values()) {
  const path = await downloadAttachment(att)
  const kb = (att.size / 1024).toFixed(0)
  lines.push(`  ${path}  (${safeAttName(att)}, ${att.contentType ?? 'unknown'}, ${kb}KB)`)
}
```

Category: `TOOL_CALL`
Note: Replace with `POST /download_attachment`; router should fetch message, download attachments, and return saved-path metadata.

- `730`

```ts
void Promise.resolve(client.destroy()).finally(() => process.exit(0))
```

Category: `LOGIN`
Note: Remove `client.destroy()`; shutdown becomes MCP transport/process cleanup only.

- `737-739`

```ts
client.on('error', err => {
  process.stderr.write(`discord channel: client error: ${err}\n`)
})
```

Category: `EVENT_HANDLER`
Note: Remove entirely; no Discord client in fork.

- `744-800`

```ts
client.on('interactionCreate', async (interaction: Interaction) => {
```

Category: `EVENT_HANDLER`
Note: Entire button interaction flow disappears unless router forwards Discord interactions to MCP.

- `745`

```ts
if (!interaction.isButton()) return
```

Category: `EVENT_HANDLER`
Note: Router would filter interaction type if this feature is preserved.

- `746`

```ts
const m = /^perm:(allow|deny|more):([a-km-z]{5})$/.exec(interaction.customId)
```

Category: `EVENT_HANDLER`
Note: Router would parse button custom IDs.

- `749-750`

```ts
if (!access.allowFrom.includes(interaction.user.id)) {
  await interaction.reply({ content: 'Not authorized.', ephemeral: true }).catch(() => {})
```

Category: `EVENT_HANDLER`
Note: Router would enforce user allowlist and send ephemeral interaction response.

- `758`

```ts
await interaction.reply({ content: 'Details no longer available.', ephemeral: true }).catch(() => {})
```

Category: `EVENT_HANDLER`
Note: Router would send ephemeral interaction response.

- `773-785`

```ts
const row = new ActionRowBuilder<ButtonBuilder>().addComponents(
  new ButtonBuilder()
    .setCustomId(`perm:allow:${request_id}`)
    .setLabel('Allow')
    .setEmoji('✅')
    .setStyle(ButtonStyle.Success),
  new ButtonBuilder()
    .setCustomId(`perm:deny:${request_id}`)
    .setLabel('Deny')
    .setEmoji('❌')
    .setStyle(ButtonStyle.Danger),
)
await interaction.update({ content: expanded, components: [row] }).catch(() => {})
```

Category: `EVENT_HANDLER`
Note: Replace with router-side interaction update if button UI remains.

- `797-799`

```ts
await interaction
  .update({ content: `${interaction.message.content}\n\n${label}`, components: [] })
  .catch(() => {})
```

Category: `EVENT_HANDLER`
Note: Router would edit the original interaction message after allow/deny.

- `802-805`

```ts
client.on('messageCreate', msg => {
  if (msg.author.bot) return
  handleInbound(msg).catch(e => process.stderr.write(`discord: handleInbound failed: ${e}\n`))
})
```

Category: `EVENT_HANDLER`
Note: Remove from MCP fork; router already owns `messageCreate`.

- `807-884`

```ts
async function handleInbound(msg: Message): Promise<void> {
```

Category: `EVENT_HANDLER`
Note: Entire inbound message-to-MCP notification bridge likely moves to router or to a separate adapter.

- `815-817`

```ts
await msg.reply(
  `${lead} — run in Claude Code:\n\n/discord:access pair ${result.code}`,
)
```

Category: `EVENT_HANDLER`
Note: Router would send pairing prompt DM/channel reply if pairing flow stays.

- `824`

```ts
const chat_id = msg.channelId
```

Category: `EVENT_HANDLER`
Note: Router forwards channel ID in MCP inbound notification.

- `830`

```ts
const permMatch = PERMISSION_REPLY_RE.exec(msg.content)
```

Category: `EVENT_HANDLER`
Note: Router can intercept text-based permission replies before relaying chat.

- `840`

```ts
void msg.react(emoji).catch(() => {})
```

Category: `EVENT_HANDLER`
Note: Router would add allow/deny ack reaction if text permission replies stay supported.

- `845-846`

```ts
if ('sendTyping' in msg.channel) {
  void msg.channel.sendTyping().catch(() => {})
}
```

Category: `EVENT_HANDLER`
Note: Router would send typing indicator before forwarding to MCP, if desired.

- `852`

```ts
void msg.react(access.ackReaction).catch(() => {})
```

Category: `EVENT_HANDLER`
Note: Router would apply configured ack reaction on inbound receipt.

- `859-861`

```ts
for (const att of msg.attachments.values()) {
  const kb = (att.size / 1024).toFixed(0)
  atts.push(`${safeAttName(att)} (${att.contentType ?? 'unknown'}, ${kb}KB)`)
}
```

Category: `EVENT_HANDLER`
Note: Router would enumerate attachments and populate MCP notification metadata.

- `866`

```ts
const content = msg.content || (atts.length > 0 ? '(attachment)' : '')
```

Category: `EVENT_HANDLER`
Note: Router would compute fallback content for attachment-only messages.

- `874-877`

```ts
message_id: msg.id,
user: msg.author.username,
user_id: msg.author.id,
ts: msg.createdAt.toISOString(),
```

Category: `EVENT_HANDLER`
Note: Router would supply this metadata in `notifications/claude/channel`.

- `886-888`

```ts
client.once('ready', c => {
  process.stderr.write(`discord channel: gateway connected as ${c.user.tag}\n`)
})
```

Category: `LOGIN`
Note: Remove entirely; no gateway login in fork.

- `890-893`

```ts
client.login(TOKEN).catch(err => {
  process.stderr.write(`discord channel: login failed: ${err}\n`)
  process.exit(1)
})
```

Category: `LOGIN`
Note: Replace with router endpoint/health validation at startup if desired; no bot token in MCP fork.

## 4. Code that must STAY (MCP scaffolding)

These parts are not direct Discord SDK logic and should be preserved verbatim or near-verbatim in the fork unless the HTTP proxy boundary forces a small signature change.

- MCP server construction: lines `437-464`
  - `new Server(...)`
  - capabilities block
  - experimental capability declarations
  - instructions string
- `StdioServerTransport` connection: line `720`
  - `await mcp.connect(new StdioServerTransport())`
- `mcp.setRequestHandler(ListToolsRequestSchema, ...)`: lines `517-596`
  - Includes the canonical tool list and schemas.
- Tool schema definitions: lines `518-595`
  - This is the authoritative source for the 5-tool catalog and should be copied exactly.
- `mcp.setRequestHandler(CallToolRequestSchema, ...)` dispatch shell: lines `598-718`
  - Keep the request dispatch shape and outer error wrapper.
  - Replace the internals of each case with router HTTP calls.
- Type definitions / interfaces:
  - `PendingEntry` lines `92-98`
  - `GroupPolicy` lines `100-103`
  - `Access` lines `105-121`
  - `GateResult` lines `215-218`
- Helper functions that do not inherently require Discord SDK:
  - `defaultAccess()` lines `123-130`
  - `assertSendable()` lines `139-149`
  - `readAccessFile()` lines `151-172`
  - `loadAccess()` lines `191-193`
  - `saveAccess()` lines `195-201`
  - `pruneExpired()` lines `203-213`
  - `noteSent()` lines `225-232`
  - `chunk()` lines `371-390`
  - `safeAttName()` lines `433-435`
- `access.json` loading logic:
  - state dir/env path constants lines `37-40`
  - `.env` file loader lines `42-51`
  - static mode boot snapshot lines `177-189`
  - access load/save/prune helpers lines `151-213`
- `pendingPermissions` map: line `467`
  - The map itself is only data.
  - It is currently used by Discord permission-request delivery and interaction handlers, so whether it stays depends on whether permission relay remains in the fork.
- Shutdown/process wiring that is not tied to Discord client lifecycle:
  - `shuttingDown` guard lines `724-729`
  - `process.stdin.on('end'/'close', shutdown)` lines `732-733`
  - `process.on('SIGTERM'/'SIGINT', shutdown)` lines `734-735`
  - Remove the `client.destroy()` call at line `730`.

## 5. Subtle items requiring design decisions

### `interactionCreate` handler (`perm:allow|deny|more` buttons)

- Original behavior:
  - The MCP server receives `notifications/claude/channel/permission_request`.
  - It stores details in `pendingPermissions`.
  - It DMs every allowlisted user a Discord message with buttons: `See more`, `Allow`, `Deny`.
  - On `interactionCreate`, it:
    - rejects unauthorized clickers
    - expands details for `more`
    - sends MCP notification `notifications/claude/channel/permission` for `allow` or `deny`
    - updates the Discord message to remove buttons
- Question for Opus:
  - Do we keep Discord button-based permission relay at all?
  - If yes, router must forward `interactionCreate` events or fully own permission-request delivery and response handling.
  - If no, can we rely entirely on the text reply path (`yes xxxxx` / `no xxxxx`) or Claude Code UI-native permission flow?

### Pairing flow / `access.json` mutation from Discord events

- Original behavior:
  - Inbound DM from a non-allowlisted sender can create a pending pairing entry in `access.json` inside `gate(...)` at lines `248-271`.
  - `pruneExpired(...)` removes expired pending entries.
  - `checkApprovals()` polls `approved/<senderId>` files created by `/discord:access` and sends a Discord confirmation message to the stored DM channel.
  - `handleInbound(...)` also replies with `/discord:access pair <code>` when pairing is needed.
- This contradicts any assumption that the plugin only reads access state. It does mutate `access.json` in response to Discord messages.
- Question for Opus:
  - Does the custom MCP fork still own pairing/access-file mutation, or must router take over all pairing-related runtime state because the Gateway event source now lives there?

### Shutdown semantics

- Original behavior:
  - `process.stdin.on('end', shutdown)`
  - `process.stdin.on('close', shutdown)`
  - `process.on('SIGTERM', shutdown)`
  - `process.on('SIGINT', shutdown)`
  - `shutdown()` logs, schedules `process.exit(0)`, and calls `client.destroy()`.
- Question for Opus:
  - In the fork, should shutdown only close MCP/stdio resources and exit immediately?
  - Is a health-check or best-effort router disconnect notification needed, or is plain process exit enough?

### Bot token requirement

- Original behavior:
  - Requires `DISCORD_BOT_TOKEN` at startup and exits if absent.
- Fork implication:
  - MCP server should not need the bot token if router owns Discord login.
- Question for Opus:
  - What env vars replace it?
  - Likely candidates:
    - `DISCORD_ROUTER_URL`
    - optional shared auth secret such as `DISCORD_ROUTER_TOKEN`
    - optional request timeout setting

### `fetch_messages` formatting responsibility

- Original behavior:
  - Tool handler fetches raw Discord messages and formats them into one plain text blob in MCP.
- Question for Opus:
  - Should router return raw structured history and let the MCP fork preserve original formatting logic verbatim?
  - Or should router return already formatted rows to keep the MCP side thinner?
- Recommendation:
  - Prefer structured router responses so the MCP fork can preserve user-visible behavior exactly.

### `noteSent` / reply-to-bot mention detection

- Original behavior:
  - `reply` stores sent message IDs in `recentSentIds`.
  - `isMentioned(...)` uses that cache so reply-to-bot counts as a mention without always fetching the referenced message.
- Question for Opus:
  - If inbound message handling stays in router, should `recentSentIds` move there too?
  - If not, router would need another way to know whether an inbound reply references a bot-authored message.

### `downloadAttachment(...)` locality

- Original behavior:
  - The MCP server downloads the attachment URL directly and writes into local `inbox/`.
- Question for Opus:
  - Should the router save files locally and return absolute paths?
  - Or should router return signed/raw attachment URLs and let MCP write them locally?
- Recommendation:
  - Router writes files locally and returns absolute paths, because the goal is to proxy all Discord operations through the router and keep MCP behavior unchanged for downstream `Read`.

### `fetchAllowedChannel(...)` enforcement boundary

- Original behavior:
  - Each tool independently checks the target channel against local `access.json`.
- Question for Opus:
  - Should access enforcement live in both places, or only in router?
- Recommendation:
  - Router should enforce authoritatively. MCP-side duplicate checks are only useful if `access.json` remains shared and local.

## 6. Proposed router HTTP API (draft)

Minimal draft based on the 5 tool requirements. Schemas are expressed as JSON-shape descriptions, not code.

### `POST /reply`

- Request body JSON schema:
  - `chat_id`: string, required
  - `text`: string, required
  - `reply_to`: string, optional
  - `files`: array of string absolute paths, optional, default `[]`
- Response body JSON schema:
  - Success:
    - `ok`: true
    - `message_ids`: array of string
    - `parts_sent`: number
  - Error:
    - `ok`: false
    - `error`: string
- Router internals needed:
  - access check equivalent to `fetchAllowedChannel`
  - `client.get_channel(...)` or fetch-equivalent
  - send message(s), optional `message_reference`, optional file attachments
  - enforce 10-file limit and 25MB per-file limit
  - return sent message IDs

### `POST /react`

- Request body JSON schema:
  - `chat_id`: string, required
  - `message_id`: string, required
  - `emoji`: string, required
- Response body JSON schema:
  - Success:
    - `ok`: true
  - Error:
    - `ok`: false
    - `error`: string
- Router internals needed:
  - access check
  - fetch message by channel + message ID
  - add reaction

### `POST /edit_message`

- Request body JSON schema:
  - `chat_id`: string, required
  - `message_id`: string, required
  - `text`: string, required
- Response body JSON schema:
  - Success:
    - `ok`: true
    - `message_id`: string
  - Error:
    - `ok`: false
    - `error`: string
- Router internals needed:
  - access check
  - fetch message
  - edit message content

### `POST /fetch_messages`

- Request body JSON schema:
  - `channel`: string, required
  - `limit`: number, optional, default `20`, max `100`
- Response body JSON schema:
  - Success:
    - `ok`: true
    - `messages`: array of objects
      - `id`: string
      - `author_id`: string
      - `author_username`: string
      - `is_me`: boolean
      - `content`: string
      - `created_at`: string ISO timestamp
      - `attachment_count`: number
  - Error:
    - `ok`: false
    - `error`: string
- Router internals needed:
  - access check
  - fetch recent channel history
  - reverse to oldest-first or return newest-first with flag; MCP can preserve exact formatting

### `POST /download_attachment`

- Request body JSON schema:
  - `chat_id`: string, required
  - `message_id`: string, required
- Response body JSON schema:
  - Success:
    - `ok`: true
    - `attachments`: array of objects
      - `path`: string absolute path
      - `name`: string
      - `content_type`: string or null
      - `size_bytes`: number
    - `message_has_attachments`: boolean
  - Error:
    - `ok`: false
    - `error`: string
- Router internals needed:
  - access check
  - fetch message
  - iterate attachments
  - enforce size limit
  - download attachment content
  - write into local inbox directory
  - return saved path metadata

### Cross-cutting router API notes

- All endpoints should probably accept an auth mechanism:
  - header bearer token, mTLS, or loopback-only binding plus shared secret
- All error responses should normalize to:

```json
{ "ok": false, "error": "..." }
```

- If the goal is same MCP semantics, the MCP fork should translate router responses back into the original MCP `content: [{ type: "text", text: ... }]` shapes exactly.
