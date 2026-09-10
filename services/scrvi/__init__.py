"""
scrVi — Discord message scraper + AI-assisted analyst.

Self-contained service:
  page  : /scrvi
  api   : /api/scrvi/...

Discord tokens and AI API keys are NOT stored server-side: the client keeps
them in localStorage and sends them with each request. Scraped messages are
held in memory (per running job) and can be pulled by the client at any time
via /api/scrvi/scrape_data/<job_id>; the browser is responsible for turning
that into a downloaded .json file at a location the user picks.
"""
import base64
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify, render_template, request

try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    genai = None
    GENAI_AVAILABLE = False

log = logging.getLogger("scrvi")

# --------------------------------------------
# BLUEPRINT
# --------------------------------------------
scrvi_bp = Blueprint(
    "scrvi",
    __name__,
    template_folder="templates",
)

_SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------
# MODEL CATALOG
# --------------------------------------------
MODELS = {
    "google": [
        {"name": "Gemini 3.8 Flash", "id": "gemini-3.8-flash"},
        {"name": "Gemini 3.7 Flash", "id": "gemini-3.7-flash"},
        {"name": "Gemini 3.6 Flash", "id": "gemini-3.6-flash"},
        {"name": "Gemini 3.5 Flash", "id": "gemini-3.5-flash"},
        {"name": "Gemini 3.5 Flash-Lite", "id": "gemini-3.5-flash-lite"},
        {"name": "Gemini 3.1 Flash-Lite", "id": "gemini-3.1-flash-lite"},
        {"name": "Gemini 3 Flash (Preview)", "id": "gemini-3-flash-preview"},
        {"name": "Gemini 2.5 Pro", "id": "gemini-2.5-pro"},
        {"name": "Gemini 2.5 Flash", "id": "gemini-2.5-flash"},
        {"name": "Gemini 2.5 Flash-Lite", "id": "gemini-2.5-flash-lite"},
    ],
    "openrouter": [
        {"name": "MiniMax: MiniMax M3 (free)", "id": "minimax/minimax-m3:free"},
        {"name": "NVIDIA: Nemotron 3 Ultra (free)", "id": "nvidia/nemotron-3-ultra-550b-a55b:free"},
        {"name": "NVIDIA: Nemotron 3.5 Lightning (free)", "id": "nvidia/nemotron-3.5-lightning:free"},
        {"name": "Thinking Machines: Inkling (free)", "id": "thinkingmachines/inkling:free"},
        {"name": "Thinking Machines: Inkling Small (free)", "id": "thinkingmachines/inkling-small:free"},
        {"name": "Dots Studio: Dots3-Note Preview (free)", "id": "dots-studio/dots-3-note-preview:free"},
        {"name": "Poolside: Laguna S 2.1 (free)", "id": "poolside/laguna-s-2.1:free"},
        {"name": "Poolside: Laguna XS 2.1 (free)", "id": "poolside/laguna-xs-2.1:free"},
        {"name": "NVIDIA: Nemotron 3 Super (free)", "id": "nvidia/nemotron-3-super-120b-a12b:free"},
        {"name": "inclusionAI: Ling 3.0 Flash Fin (free)", "id": "inclusionai/ling-3.0-flash-fin:free"},
        {"name": "inclusionAI: Ling 3.0 Flash Sante (free)", "id": "inclusionai/ling-3.0-flash-sante:free"},
        {"name": "NVIDIA: Nemotron 3 Nano Omni (free)", "id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"},
        {"name": "Z.ai: GLM 5.2 (free)", "id": "z-ai/glm-5.2:free"},
        {"name": "MiniMax: MiniMax M2.7 (free)", "id": "minimax/minimax-m2.7:free"},
        {"name": "Google: Gemma 4 26B A4B (free)", "id": "google/gemma-4-26b-a4b-it:free"},
        {"name": "Google: Gemma 4 31B (free)", "id": "google/gemma-4-31b-it:free"},
        {"name": "Cohere: North Mini Code (free)", "id": "cohere/north-mini-code:free"},
        {"name": "NVIDIA: Nemotron 3.5 Content Safety (free)", "id": "nvidia/nemotron-3.5-content-safety:free"},
    ],
}

# --------------------------------------------
# BACKGROUND IMAGE
# --------------------------------------------
bkg_data = ""
bkg_path = os.path.join(_SERVICE_DIR, "bkg.png")
if os.path.exists(bkg_path):
    try:
        with open(bkg_path, "rb") as f:
            bkg_data = base64.b64encode(f.read()).decode("utf-8")
    except OSError as e:
        log.warning("Could not load background image (%s): %s", bkg_path, e)

if bkg_data:
    data_uri = f"url('data:image/png;base64,{bkg_data}')"
    BKG_STYLE = f"background-image: {data_uri}, {data_uri}; background-position: 0 0, 6688px 0;"
else:
    BKG_STYLE = "background: linear-gradient(145deg, #111214 0%, #1a1b1e 100%);"


# --------------------------------------------
# SCRAPER JOB (in-memory only — no disk writes)
# --------------------------------------------
class ScraperJob:
    def __init__(self, job_id, token, guild_id, user_id):
        self.id = job_id
        self.token = token
        self.guild_id = guild_id
        self.user_id = user_id
        self.running = False
        self.done = False
        self.stopped = False
        self.progress = 0
        self.total = 0
        self.messages = []
        self.user_info = None
        self.error = None
        self.log_lines = []

    def add_log(self, msg):
        self.log_lines.append(msg)
        log.info("[job %s] %s", self.id, msg)

    def stop(self):
        self.stopped = True

    def snapshot(self):
        """Return a JSON-serializable snapshot of the current scrape."""
        username = (
            (self.user_info.get("global_name") or self.user_info.get("username"))
            if self.user_info else None
        ) or f"user_{self.user_id}"
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", username)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return {
            "user": self.user_info or {
                "id": str(self.user_id),
                "username": "unknown",
                "global_name": None,
            },
            "guild_id": str(self.guild_id),
            "scraped_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "total_expected": self.total,
            "total_scraped": len(self.messages),
            "messages": self.messages,
            "suggested_filename": f"{safe}_{ts}.json",
        }

    def run(self):
        self.running = True
        self.add_log(f"🔍 Starting scrape for user {self.user_id} in guild {self.guild_id}")
        try:
            headers = {
                "Authorization": self.token,
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"),
                "X-Super-Properties": base64.b64encode(
                    json.dumps({
                        "os": "Windows",
                        "browser": "Chrome",
                        "device": "",
                        "system_locale": "en-US",
                        "browser_user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                                               "Chrome/120.0.0.0 Safari/537.36"),
                        "browser_version": "120.0.0.0",
                        "os_version": "10",
                        "referrer": "",
                        "referring_domain": "",
                        "referrer_current": "",
                        "referring_domain_current": "",
                        "release_channel": "stable",
                        "client_build_number": 123456,
                        "client_event_source": None,
                    }).encode()
                ).decode(),
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://discord.com/channels/@me",
                "Origin": "https://discord.com",
                "DNT": "1",
                "Connection": "keep-alive",
            }

            max_id = None
            seen_ids = set()
            total_expected = 0
            batch_count = 0

            while not self.stopped:
                batch_count += 1
                self.add_log(f"   Batch {batch_count}...")

                url = f"https://discord.com/api/v9/guilds/{self.guild_id}/messages/search"
                params = {"author_id": self.user_id, "limit": 25, "include_nsfw": "true"}
                if max_id:
                    params["max_id"] = max_id

                resp = requests.get(url, headers=headers, params=params)

                if resp.status_code == 200:
                    data = resp.json()
                    total_results = data.get("total_results", 0)
                    if total_expected == 0:
                        total_expected = total_results
                        self.total = total_expected
                        self.add_log(f"   Found {total_results} messages")

                    new_msgs = []
                    for msg_group in data.get("messages", []):
                        for msg in msg_group:
                            author = msg.get("author", {})
                            if str(author.get("id")) == str(self.user_id):
                                if self.user_info is None:
                                    self.user_info = {
                                        "id": str(author.get("id")),
                                        "username": author.get("username", "unknown"),
                                        "global_name": author.get("global_name"),
                                    }
                                msg_id = msg.get("id")
                                if msg_id not in seen_ids:
                                    seen_ids.add(msg_id)
                                    new_msgs.append({
                                        "id": msg_id,
                                        "timestamp": msg.get("timestamp"),
                                        "content": msg.get("content"),
                                        "channel_id": msg.get("channel_id"),
                                        "attachments": msg.get("attachments", []),
                                        "embeds": msg.get("embeds", []),
                                    })
                    if new_msgs:
                        self.messages.extend(new_msgs)
                        self.progress = len(self.messages)
                        self.add_log(f"   +{len(new_msgs)} messages (total: {len(self.messages)}/{total_expected})")

                    if self.messages:
                        oldest = min(self.messages, key=lambda m: int(m["id"]))
                        next_max_id = oldest["id"]
                        if next_max_id == max_id:
                            break
                        max_id = next_max_id
                    else:
                        break

                    if len(self.messages) >= total_expected:
                        self.add_log("✅ All messages scraped!")
                        break

                    time.sleep(0.5)

                elif resp.status_code == 202:
                    retry_after = resp.json().get("retry_after", 5)
                    self.add_log(f"      ⏳ Guild indexing. Retry after {retry_after}s...")
                    time.sleep(retry_after)

                elif resp.status_code == 429:
                    self.add_log("      ⏳ Rate limited. Waiting 15 seconds...")
                    for _ in range(15, 0, -1):
                        if self.stopped:
                            break
                        time.sleep(1)
                    self.add_log("      ⏳ Retrying...")

                elif resp.status_code == 401:
                    self.add_log("      ❌ Authentication failed. Check your token.")
                    self.error = "Authentication failed"
                    break

                else:
                    self.add_log(f"      ❌ API Error: {resp.status_code} - {resp.text}")
                    self.error = f"API error {resp.status_code}"
                    break

            if self.stopped:
                self.add_log(f"⏹️ Scrape stopped by user ({len(self.messages)} messages collected).")
            else:
                self.add_log(f"✅ Scrape finished ({len(self.messages)} messages collected).")
            self.done = True
        except Exception as e:
            self.add_log(f"❌ Scrape error: {e}")
            self.error = str(e)
        finally:
            self.running = False


scraper_jobs: dict = {}
job_counter = 0
_jobs_lock = threading.Lock()
_gemini_lock = threading.Lock()


# --------------------------------------------
# ROUTES
# --------------------------------------------
@scrvi_bp.route("/scrvi")
def scrvi_ui():
    return render_template(
        "scrvi.html",
        bkg_style=BKG_STYLE,
        models_json=json.dumps(MODELS),
    )


@scrvi_bp.route("/api/scrvi/start_scrape", methods=["POST"])
def api_start_scrape():
    global job_counter
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    guild_id = (data.get("guild_id") or "").strip()
    user_id = (data.get("user_id") or "").strip()
    if not token or not guild_id or not user_id:
        return jsonify({"error": "token, guild_id and user_id are all required"}), 400

    with _jobs_lock:
        job_counter += 1
        job_id = str(job_counter)

    job = ScraperJob(job_id, token, guild_id, user_id)
    scraper_jobs[job_id] = job
    threading.Thread(target=job.run, daemon=True).start()
    return jsonify({"job_id": job_id})


@scrvi_bp.route("/api/scrvi/stop_scrape", methods=["POST"])
def api_stop_scrape():
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id") or "")
    job = scraper_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if not job.running:
        return jsonify({"error": "Job is not running"}), 400
    job.stop()
    return jsonify({"success": True})


@scrvi_bp.route("/api/scrvi/scrape_status/<job_id>")
def api_scrape_status(job_id):
    job = scraper_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "running": job.running,
        "done": job.done,
        "stopped": job.stopped,
        "progress": job.progress,
        "total": job.total,
        "error": job.error,
        "logs": job.log_lines,
    })


@scrvi_bp.route("/api/scrvi/scrape_data/<job_id>")
def api_scrape_data(job_id):
    """Return the current messages of a job. Works while running too, so
    the client can download or load partial results at any point."""
    job = scraper_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if not job.messages:
        return jsonify({"error": "No messages scraped yet"}), 400
    return jsonify(job.snapshot())


@scrvi_bp.route("/api/scrvi/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json(silent=True) or {}
    provider = data.get("provider")
    model_id = data.get("model_id")
    api_key = (data.get("api_key") or "").strip()
    messages_text = data.get("messages_text") or ""

    if not messages_text.strip():
        return jsonify({"success": False, "error": "No messages loaded. Please import a JSON file first."})
    if not api_key:
        return jsonify({"success": False, "error": f"API key for '{provider}' is not set. Save it in the AI tab."})

    if len(messages_text) > 250_000:
        messages_text = messages_text[:250_000] + "\n[... truncated due to length limit]\n"

    system_prompt = (
        "You are an intelligence analyst. You are given a list of messages from a single person, "
        "each with a UTC timestamp. Your task is to build a comprehensive profile of this person "
        "from the content of these messages. Extract every piece of personal information that is "
        "explicitly stated or can be reliably inferred. Pay special attention to:\n\n"
        "• **Location**: if the person mentions a local time or a city/country that they live in (e.g., 'it's 4pm here'), cross‑reference "
        "with the UTC timestamp to determine their approximate timezone/location.\n"
        "• **Employment/Education**: look for clues about their job, student status, career aspirations, "
        "or work schedule.\n"
        "• **Languages**: detect which languages they use (including code‑switching) and estimate their "
        "proficiency (native, fluent, intermediate, basic).\n"
        "• **Personal details**: age, gender, location (city/country), family, relationships, hobbies, "
        "interests, health issues, problems they discuss, etc.\n"
        "• **Lifestyle**: sleep patterns, daily routine, social habits, etc.\n"
        "• **Any other notable patterns**: tone, personality, emotional state, recurring themes.\n\n"
        "Be thorough but objective. If something is uncertain, state that it is an inference. "
        "Remember that some messages may not be about the person themselves (e.g., they could be "
        "quoting others or discussing third parties) – be careful not to attribute those traits to "
        "the subject unless there is clear context. The goal is to know as much as possible about "
        "the subject from these messages alone. Present your findings as a bulleted list with "
        "clear headings for each category."
        "AFTER finding/deducing everything about the subject at the END of the response leave a summary of pure facts about the subject."
    )
    user_prompt = (
        f"Here are the messages with UTC timestamps:\n{messages_text}\n\n"
        "Analyze and provide a bulleted list of facts."
    )

    if provider == "google":
        if not GENAI_AVAILABLE:
            return jsonify({"success": False, "error": "google-generativeai is not installed on the server."})
        try:
            with _gemini_lock:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel(model_id, system_instruction=system_prompt or None)
                response = model.generate_content(
                    user_prompt,
                    generation_config=genai.types.GenerationConfig(
                        temperature=0.3,
                        max_output_tokens=6000,
                    ),
                )
            if not response.candidates:
                return jsonify({"success": False, "error": "Gemini returned no candidates (likely blocked by safety filters)."})
            return jsonify({"success": True, "result": response.text.strip()})
        except Exception as e:
            return jsonify({"success": False, "error": f"Error calling google API: {e}"})

    if provider == "openrouter":
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 6000,
        }
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=60,
            )
            resp.raise_for_status()
            rdata = resp.json()
            choices = rdata.get("choices") or []
            if not choices:
                err = rdata.get("error", "unknown error")
                return jsonify({"success": False, "error": f"openrouter returned no choices ({err})."})
            content = (choices[0].get("message") or {}).get("content", "").strip()
            if choices[0].get("finish_reason") == "length":
                content += "\n\n[note: response was cut off at the max_tokens limit — increase max_tokens if you need the full output]"
            return jsonify({"success": True, "result": content})
        except requests.exceptions.Timeout:
            return jsonify({"success": False, "error": "Error calling openrouter API: request timed out."})
        except requests.exceptions.RequestException as e:
            return jsonify({"success": False, "error": f"Error calling openrouter API: {e}"})
        except (KeyError, ValueError) as e:
            return jsonify({"success": False, "error": f"Error parsing openrouter response: {e}"})

    return jsonify({"success": False, "error": f"Unsupported provider: {provider}"})