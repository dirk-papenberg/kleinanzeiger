# Kleinanzeigen Agent

A Telegram bot that turns photos into Kleinanzeigen.de ad drafts using Claude Vision AI.

## Features

- **AI-Powered Draft Generation**: Send photos and Claude analyzes them to create realistic ad listings
- **Smart Editing**: Edit drafts with natural language ("raise price to 30 EUR", "make description shorter")
- **Immediate Feedback**: Users get instant acknowledgment when sending photos
- **Background Processing**: Ad publishing runs in a background queue - no waiting!
- **Session Management**: After queuing a job, the session resets so you can prepare your next ad
- **Persistent Queue**: Failed jobs are stored in a backout queue for later retry
- **Retry Logic**: Automatic retries for transient failures, manual retry for persistent issues
- **Direct Publishing**: Optionally publish ads directly to Kleinanzeigen (requires `kleinanzeigen-bot`)

## Quick Start

### Prerequisites

- Python 3.12+
- Telegram Bot Token (from @BotFather)
- Anthropic API Key or AWS Bedrock credentials
- Optional: `kleinanzeigen-bot` binary for direct publishing

### Installation

```bash
# Clone or setup the project
cd kleinanzeiger

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt
```

### Configuration

Create a `.env` file:

```env
# Required
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# Optional: AWS Bedrock (instead of Anthropic)
# LLM_PROVIDER=bedrock
# AWS_BEARER_TOKEN_BEDROCK=your_token_here
# AWS_REGION=us-east-1

# Optional: Direct publishing to Kleinanzeigen
KLEINANZEIGEN_BOT_CMD=/path/to/kleinanzeigen-bot
KLEINANZEIGEN_CONFIG=~/.kleinanzeigen-agent/config.yaml
KLEINANZEIGEN_USERNAME=your_username
KLEINANZEIGEN_PASSWORD=your_password
KLEINANZEIGEN_SHIPPING=PICKUP

# Optional: Storage directories
KLEINANZEIGEN_WORK_DIR=~/.kleinanzeigen-agent/ads
KLEINANZEIGEN_QUEUE_DIR=~/.kleinanzeigen-agent/queue
```

### Running the Bot

```bash
python main.py
```

The bot will:
1. Start listening for messages
2. Initialize the queue system
3. Start the background worker for processing jobs

## Usage

### Basic Workflow

1. **Send Photos**: Send one or more photos of the item you want to sell
   - For albums, send multiple photos at once or in quick succession
   - Bot acknowledges immediately with "📸 Erhalten! Analysiere…"
   - Claude analyzes and generates draft listing

2. **Review Draft**: Bot shows the generated listing with options:
   - ✅ **Copy-Paste**: Get formatted text for manual posting
   - 🔁 **Neu generieren**: Generate a different draft for the same photos
   - 🚀 **Direkt schalten**: Queue for direct publishing (if configured)
   - ❌ **Verwerfen**: Discard draft

3. **Edit (Optional)**: Reply with natural language edits:
   - "Preis auf 30 EUR erhöhen"
   - "Beschreibung kürzer machen"
   - "Erwähne dass es noch in OVP ist"
   - "Formeller schreiben"

4. **Publish**: Choose your publishing method:
   - **Copy-Paste**: Manual posting via Kleinanzeigen app
   - **Direct**: Queue for automatic publishing (requires setup)

### Commands

- `/start` or `/help` - Show welcome message
- `/neu` - Discard current draft and start fresh
- `/queue` - Check queue status (pending/backout jobs)
- `/backout` - List failed jobs for this chat
- `/retry_<job_id>` - Retry a specific failed job

Example: `/retry_a1b2c3d4` (use the 8-character job ID from `/backout`)

## Queue System

The system uses a persistent queue for ad publishing:

### Immediate User Feedback

When you click "Direkt schalten":
- ✅ Bot shows: "⏳ Anzeige wird geschaltet… [job-id]"
- ✅ Session resets: "Du kannst jetzt das nächste Inserat vorbereiten."
- ✅ You can immediately send new photos for the next ad
- ✅ Bot will notify you when publishing completes

### Background Processing

- Publishing happens in the background - no blocking!
- Jobs persist across bot restarts
- Failed jobs are automatically retried (up to 3 times)
- Persistent failures go to backout queue

### Job Management

```bash
# Check what's in the queue
/queue

# See failed jobs
/backout

# Retry a failed job
/retry_a1b2c3d4
```

## Configuration Details

### LLM Provider Selection

The bot auto-detects which LLM provider to use:

1. **Anthropic** (default):
   - Set `ANTHROPIC_API_KEY`
   - Optional: `CLAUDE_MODEL` (default: claude-sonnet-4-6)

2. **AWS Bedrock**:
   - Set `LLM_PROVIDER=bedrock`
   - Set `AWS_BEARER_TOKEN_BEDROCK`
   - Set `CLAUDE_MODEL` to inference profile ID

### Direct Publishing Setup

To enable the "🚀 Direkt schalten" button:

1. Install `kleinanzeigen-bot`:
   ```bash
   pip install kleinanzeigen-bot
   ```

2. Create config at `~/.kleinanzeigen-agent/config.yaml`:
   ```yaml
   login:
     username: your_email@example.com
     password: your_password
   ```

3. Set environment variable:
   ```env
   KLEINANZEIGEN_BOT_CMD=kleinanzeigen-bot
   ```

4. Test:
   ```bash
   kleinanzeigen-bot --help
   ```

### Storage Directories

- **Ads**: `~/.kleinanzeigen-agent/ads/` (photos + ad.yaml files)
- **Queue**: `~/.kleinanzeigen-agent/queue/` (job persistence)

## Architecture

### Three Main Modules

1. **main.py**
   - Telegram bot handlers (messages, callbacks, commands)
   - Job handlers for different operations
   - User interface and interactions

2. **queue_manager.py**
   - Job queue with file-based persistence
   - Job lifecycle management (pending → processing → completed/backout)
   - Retry logic and status tracking

3. **background_worker.py**
   - Background async task that processes queued jobs
   - Handles job execution and error recovery
   - Notifies users on completion

### Data Flow

```
User sends photo
    ↓
    [Immediate: "📸 Erhalten! Analysiere…"]
    [Analysis: "🤖 Beginne Bildanalyse…"]
    ↓
Claude analyzes
    ↓
Bot shows draft
    ↓
User clicks "Direkt schalten"
    ↓
    [Immediate: "⏳ Anzeige wird geschaltet…"]
    [Reset: "Du kannst jetzt das nächste Inserat vorbereiten."]
    ↓
Job queued
    ↓
Background worker processes
    ↓
    [Result: "✅ Anzeige geschaltet!" or "❌ Fehler…"]
```

## Error Handling

### Automatic Retries

Failed jobs are automatically retried up to 3 times:
- Network errors → retry
- Temporary bot issues → retry
- Persistent failures → backout queue

### Manual Retry

If a job still fails after automatic retries:
1. Check `/backout` to see failed jobs
2. Review the error message
3. Fix any issues (credentials, config, etc.)
4. Retry with `/retry_<job_id>`

### Common Issues

| Error | Cause | Solution |
|-------|-------|----------|
| "Kleinanzeigen-Login unvollständig" | Missing credentials | Set KLEINANZEIGEN_USERNAME/PASSWORD |
| "Direktes Schalten ist nicht konfiguriert" | KLEINANZEIGEN_BOT_CMD not set | Install kleinanzeigen-bot and configure |
| "Fehler bei der Analyse" | Claude API error | Check API key and Anthropic status |
| Job stuck in pending | Worker crashed | Check logs, restart bot |

## Development

### Adding New Job Types

1. Create handler in `main.py`:
```python
async def handle_my_task(job_data: dict) -> tuple[bool, str]:
    try:
        # Do work...
        return True, "Success"
    except Exception as e:
        return False, f"Error: {e}"
```

2. Register in `job_handlers`:
```python
job_handlers = {
    "publish_ad": handle_publish_job,
    "my_task": handle_my_task,
}
```

3. Queue jobs:
```python
QUEUE_MANAGER.enqueue(
    job_id=str(uuid.uuid4()),
    chat_id=chat_id,
    job_type="my_task",
    data={"key": "value"},
)
```

### Queue System

See [QUEUE_SYSTEM.md](QUEUE_SYSTEM.md) for detailed queue architecture and implementation.

## Requirements

- Python 3.12+
- python-telegram-bot >= 21.6
- anthropic >= 0.40.0
- python-dotenv >= 1.0.0

Optional:
- kleinanzeigen-bot (for direct publishing)

## Troubleshooting

### Bot not responding

1. Check Telegram Bot Token
2. Check bot is running: `python main.py`
3. Check logs for errors

### Photos not analyzing

1. Verify ANTHROPIC_API_KEY is set and valid
2. Check network connectivity
3. Check Anthropic API status
4. Review logs for error details

### Publishing fails

1. Check kleinanzeigen-bot is installed: `kleinanzeigen-bot --help`
2. Verify config file exists and credentials are correct
3. Test manually: `kleinanzeigen-bot publish --ads new`
4. Check logs for specific error
5. Use `/backout` to see failed job details

### Queue issues

1. Check `~/.kleinanzeigen-agent/queue/` directory
2. Review JSONL files for corrupted entries
3. Check background worker logs
4. Restart bot to resume processing

## Support

For issues with:
- **Telegram Bot**: Check python-telegram-bot documentation
- **Claude AI**: Check Anthropic documentation
- **Kleinanzeigen Bot**: Check kleinanzeigen-bot repository

## License

[Add your license here]

## Changelog

### v0.2.0 (Queue System Update)
- ✅ Immediate user feedback on photo upload
- ✅ Background job processing for ad publishing
- ✅ Persistent job queue with file storage
- ✅ Automatic retry with backout queue
- ✅ User notifications on job completion
- ✅ New commands: `/queue`, `/backout`, `/retry`
- ✅ Session reset after job queueing

### v0.1.0 (Initial Release)
- Basic photo-to-draft generation
- Draft editing with natural language
- Copy-paste publishing
- Optional direct publishing via kleinanzeigen-bot
