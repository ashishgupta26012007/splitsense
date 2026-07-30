"""
SplitSense — an autonomous expense-settling agent for groups/roommates.

Flask backend that:
  1. Accepts a plain-English description of group expenses.
  2. Uses a 4-step flow: LLM parse -> Python settlement compute -> Python
      verification -> LLM reminder drafting.
  3. Returns the result to the frontend as clean, validated JSON.

Run with:  python app.py
"""

import io
import os
from werkzeug.utils import secure_filename
import uuid
import json
import re
import logging
from decimal import Decimal, ROUND_HALF_UP

import requests
from flask import Flask, render_template, request, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from models import db, User, Expense, Transaction, Group, GroupMember, JoinRequest
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from flask import send_file
import qrcode
import base64


load_dotenv(override=True) 

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "receipts")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB max upload size

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

ALLOWED_RECEIPT_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "pdf"}


def allowed_receipt_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_RECEIPT_EXTENSIONS
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-this")
db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


with app.app_context():
    db.create_all()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("splitsense")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = os.environ.get("OPENROUTER_API_KEY")


_default_fallback_models = [
    "openrouter/free",  
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-coder:free",
    "openai/gpt-oss-120b:free",
    "google/gemma-3-27b-it:free",
    "deepseek/deepseek-chat-v3-0324:free",
]

_single_model_override = os.environ.get("OPENROUTER_MODEL")
if _single_model_override:
    CANDIDATE_MODELS = [_single_model_override]
else:
    CANDIDATE_MODELS = _default_fallback_models






PARSE_SYSTEM_PROMPT = """You are SplitSense Step 1: parsing group expenses into structured data.

You will be given free text that may describe one or more expenses.
Your ONLY job is to extract structured expenses. Do not compute balances,
do not simplify debts, and do not write reminder messages.

Return ONLY valid JSON in exactly one of these forms:

Success shape:
{
    "expense_summary": [
        {
            "description": "short description like Hotel",
            "paid_by": "Name",
            "amount": 4500.0,
            "split_between": ["Name1", "Name2", "Name3"],
            "category": "Food",
            "custom_shares": null
        }
    ]
}

Error shape:
{"error": "one short, friendly clarification message"}

Critical parsing rules:
- Every expense MUST include payer, amount, and an explicit list of participants in split_between.
- If an expense mentions a payer and amount but does not clearly name who the expense is split between,
    return the error shape asking the user to clarify the people involved. Never guess participants.
- Normalize names (trim spaces, consistent capitalization).
- Amount must be numeric and greater than 0.
- The "category" field must be exactly one of: "Food", "Travel", "Rent", "Utilities", "Entertainment", "Shopping", "Other".
    Guess the best fitting category from the description. If unsure, use "Other".
- The "custom_shares" field: if the text specifies unequal amounts per person
    (e.g. "split 600 for Raj and 400 for Simran"), return an object mapping each
    name to their exact share, e.g. {"Raj": 600.0, "Simran": 400.0}. The shares
    MUST sum to exactly the total amount. If the split is equal or unspecified,
    set "custom_shares" to null.
- If the input cannot reasonably be interpreted as expenses, return the error shape.
- Never include markdown fences or any text outside the JSON object.
"""

REMINDER_SYSTEM_PROMPT = """You are SplitSense Step 4: drafting friendly reminders for a final settlement plan.

You will receive a verified settlement_plan where each item has:
- from: person who owes
- to: person who should receive
- amount: payment amount

Return ONLY valid JSON in this exact shape:
{
    "reminders": [
        "one to two friendly sentences for transaction #1",
        "one to two friendly sentences for transaction #2"
    ]
}

Rules:
- Write exactly one reminder per transaction, in the same order.
- Address the person in "from" directly by name.
- Mention who to pay and the exact amount.
- Keep each reminder short, warm, and casual.
- If settlement_plan is empty, return {"reminders": []}.
- Never include markdown fences or text outside the JSON object.
"""


def build_parse_user_prompt(raw_text: str) -> str:
    return f"""Here are the group expenses to parse:

\"\"\"{raw_text}\"\"\"

Return only the JSON object described in your instructions."""


def build_reminder_user_prompt(settlement_plan: list[dict]) -> str:
    return (
        "Here is the verified settlement_plan to draft reminders for:\n\n"
        f"{json.dumps(settlement_plan, ensure_ascii=True)}\n\n"
        "Return only the JSON object described in your instructions."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def looks_like_gibberish_or_empty(text: str) -> str | None:
    """
    Cheap, fast, pre-LLM sanity checks so we don't waste an API call (or give
    a confusing error) on obviously unusable input.
    Returns an error message string if input is unusable, else None.
    """
    if text is None:
        return "Please enter some expenses first - the input was empty."

    cleaned = text.strip()

    if not cleaned:
        return "Please enter some expenses first - the input was empty."

    if len(cleaned) < 5:
        return "That looks too short to be an expense. Try something like: 'Raj paid 4500 for hotel for Raj, Simran, Aman'."

    if not re.search(r"\d", cleaned):
        return "I couldn't find any amounts in that text. Please include how much was paid, e.g. 'Aman paid 1200 for groceries for everyone'."

    words = re.findall(r"[A-Za-z]{2,}", cleaned)
    if len(words) < 2:
        return "I couldn't find enough detail there. Please mention who paid, how much, and who it was for."

    return None


def extract_json(raw: str) -> dict:
    """
    Claude is instructed to return raw JSON, but this defensively strips
    markdown code fences or stray text if they slip in, then parses it.
    Raises json.JSONDecodeError if it still can't be parsed.
    """
    text = raw.strip()

    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    if not text.startswith("{"):
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            text = text[first_brace:last_brace + 1]

    return json.loads(text)


class UpstreamError(Exception):
    """Raised for any error talking to OpenRouter, carrying a friendly message + HTTP status."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _call_one_model(model_id: str, system_prompt: str, user_prompt: str) -> str:
    """
    Single call to OpenRouter's OpenAI-compatible chat completions endpoint
    for one specific model id. Raises UpstreamError with a friendly message
    on any failure. A 404 here specifically means "this model id is no
    longer available for free" - the caller uses that to try the next
    candidate model.
    """
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "SplitSense",
    }
    body = {
        "model": model_id,
        "max_tokens": 2000,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        response = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=45)
    except requests.exceptions.ConnectionError:
        raise UpstreamError("Couldn't reach OpenRouter - please check your internet connection and try again.", 503)
    except requests.exceptions.Timeout:
        raise UpstreamError("OpenRouter took too long to respond. Please try again.", 504)
    except requests.exceptions.RequestException:
        logger.exception("Unexpected requests error calling OpenRouter")
        raise UpstreamError("Something went wrong while contacting the AI service. Please try again.", 500)

    if response.status_code == 401:
        raise UpstreamError("The OpenRouter API key looks invalid. Please check OPENROUTER_API_KEY in your .env file.", 401)

    if response.status_code == 429:
        raise UpstreamError("We're being rate-limited right now (free models often have tight limits). Please wait a moment and try again.", 429)

    if response.status_code == 404:
        
        logger.warning("Model unavailable, will try next candidate: %s -> %s", model_id, response.text[:300])
        raise UpstreamError(f"Model '{model_id}' is no longer available for free.", 404)

    if response.status_code == 402:
        raise UpstreamError("This model needs credits on your OpenRouter account.", 402)

    if response.status_code >= 500:
        raise UpstreamError("OpenRouter (or the model provider) is having issues right now. Please try again in a bit.", 502)

    if response.status_code >= 400:
        logger.warning("OpenRouter returned %s: %s", response.status_code, response.text[:500])
        raise UpstreamError("The AI service rejected the request.", 400)

    try:
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError):
        logger.exception("Unexpected OpenRouter response shape: %s", response.text[:500])
        raise UpstreamError("Got an unexpected response from the AI service. Please try again.", 502)


def call_llm(system_prompt: str, user_prompt: str) -> str:
    """
    Tries each model in CANDIDATE_MODELS in order. If a model returns 404
    (no longer free / doesn't exist), moves on to the next one automatically.
    Any other kind of failure (bad key, rate limit, network, etc.) is raised
    immediately since trying a different model won't fix it.
    """
    last_error = None

    for model_id in CANDIDATE_MODELS:
        try:
            return _call_one_model(model_id, system_prompt, user_prompt)
        except UpstreamError as e:
            if e.status_code in (400, 404):
                last_error = e
                continue  
            raise 

    raise UpstreamError(
        "All the configured free models are currently unavailable on OpenRouter. "
        "Please check https://openrouter.ai/models (filter by 'free') for a currently "
        "active model id and set OPENROUTER_MODEL in your .env file to it.",
        502,
    )


def validate_parse_shape(data: dict) -> str | None:
    """
    Confirms the parsed JSON has the shape the frontend expects.
    Returns an error string if invalid, else None.
    """
    if "error" in data:
        return None

    if "expense_summary" not in data or not isinstance(data["expense_summary"], list):
        return "Response was missing a valid 'expense_summary' list."

    valid_categories = {"Food", "Travel", "Rent", "Utilities", "Entertainment", "Shopping", "Other"}

    for item in data["expense_summary"]:
        if not isinstance(item, dict):
            return "Each expense summary item must be an object."
        for key in ("description", "paid_by", "amount", "split_between"):
            if key not in item:
                return f"An expense item was missing '{key}'."
        if not isinstance(item["split_between"], list) or not item["split_between"]:
            return "Each expense must include a non-empty split_between list."
        if item.get("category") not in valid_categories:
            item["category"] = "Other" 

        if "custom_shares" not in item:
            item["custom_shares"] = None

    return None


def validate_reminder_shape(data: dict, expected_count: int) -> str | None:
    if "reminders" not in data or not isinstance(data["reminders"], list):
        return "Response was missing a valid 'reminders' list."

    if len(data["reminders"]) != expected_count:
        return "The number of reminders did not match the settlement plan."

    return None


def normalize_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(name or "").strip())
    return " ".join(part.capitalize() for part in cleaned.split(" ") if part)


def unique_names_in_order(names: list[str]) -> list[str]:
    seen = set()
    unique = []
    for raw in names:
        normalized = normalize_name(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def to_cents(value: float | str | Decimal) -> int:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(amount * 100)


def cents_to_amount(cents: int) -> float:
    return float((Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_balances(expenses: list[dict]) -> tuple[dict[str, int], int]:
    """
    Returns (balances_in_cents, original_transfer_volume_in_cents).
    Supports both equal splits and custom per-person shares.
    """
    balances: dict[str, int] = {}
    original_transfer_volume_cents = 0

    for expense in expenses:
        payer = normalize_name(expense.get("paid_by", ""))
        description = str(expense.get("description", "")).strip() or "Expense"
        participants = unique_names_in_order(expense.get("split_between") or [])
        custom_shares = expense.get("custom_shares")

        if not payer:
            raise ValueError(f"Expense '{description}' is missing a valid payer name.")
        if not participants:
            raise ValueError(f"Expense '{description}' is missing split participants.")

        amount_cents = to_cents(expense.get("amount", 0))
        if amount_cents <= 0:
            raise ValueError(f"Expense '{description}' has an invalid amount.")

        balances.setdefault(payer, 0)
        balances[payer] += amount_cents

        if custom_shares and isinstance(custom_shares, dict):
            normalized_shares = {normalize_name(k): to_cents(v) for k, v in custom_shares.items()}
            shares_total = sum(normalized_shares.values())

            if shares_total != amount_cents:
                raise ValueError(
                    f"Expense '{description}': custom shares (₹{cents_to_amount(shares_total)}) "
                    f"don't add up to the total amount (₹{cents_to_amount(amount_cents)})."
                )

            for participant in participants:
                share_cents = normalized_shares.get(participant, 0)
                balances.setdefault(participant, 0)
                balances[participant] -= share_cents

                if participant != payer:
                    original_transfer_volume_cents += share_cents
        else:
            split_count = len(participants)
            base_share = amount_cents // split_count
            remainder = amount_cents % split_count

            for idx, participant in enumerate(participants):
                share_cents = base_share + (1 if idx < remainder else 0)
                balances.setdefault(participant, 0)
                balances[participant] -= share_cents

                if participant != payer:
                    original_transfer_volume_cents += share_cents

    for name, cents in list(balances.items()):
        if abs(cents) <= 0:
            balances[name] = 0

    return balances, original_transfer_volume_cents


def compute_group_running_balances(group_id: int) -> dict[str, float]:
    """
    Computes each member's live net balance across ALL expenses ever logged
    in this group, then subtracts any settlements already marked as paid.
    This gives a real-time "who owes what" view, independent of any single
    settle session.
    """
    group_expenses = Expense.query.filter_by(group_id=group_id).all()

    expense_dicts = [
        {
            "description": e.description,
            "paid_by": e.paid_by,
            "amount": e.amount,
            "split_between": [name.strip() for name in e.split_with.split(",")],
            "custom_shares": None,  
        }
        for e in group_expenses
    ]

    if not expense_dicts:
        return {}

    balances_cents, _ = compute_balances(expense_dicts)

    # Subtract already-paid transactions for this group
    paid_transactions = Transaction.query.filter_by(group_id=group_id, is_paid=True).all()
    for txn in paid_transactions:
        payer = normalize_name(txn.payer)
        receiver = normalize_name(txn.receiver)
        amount_cents = to_cents(txn.amount)

        balances_cents.setdefault(payer, 0)
        balances_cents.setdefault(receiver, 0)
        balances_cents[payer] += amount_cents
        balances_cents[receiver] -= amount_cents

    return {name: cents_to_amount(cents) for name, cents in balances_cents.items()}


def compute_settlement(balances: dict[str, int]) -> list[dict]:
    """
    Greedy debt simplification: repeatedly match max debtor with max creditor.
    Input balances are in cents: positive=should receive, negative=owes.
    Returns settlement entries with float amounts rounded to 2 decimals.
    """
    creditors = [[name, cents] for name, cents in balances.items() if cents > 0]
    debtors = [[name, -cents] for name, cents in balances.items() if cents < 0]

    creditors.sort(key=lambda x: x[1], reverse=True)
    debtors.sort(key=lambda x: x[1], reverse=True)

    i, j = 0, 0
    plan: list[dict] = []

    while i < len(debtors) and j < len(creditors):
        debtor_name, debtor_owes = debtors[i]
        creditor_name, creditor_gets = creditors[j]

        payment_cents = min(debtor_owes, creditor_gets)
        if payment_cents <= 0:
            break

        plan.append({
            "from": debtor_name,
            "to": creditor_name,
            "amount": cents_to_amount(payment_cents),
        })

        debtors[i][1] -= payment_cents
        creditors[j][1] -= payment_cents

        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1

    return plan


def verify_settlement(
    balances: dict[str, int],
    settlement_plan: list[dict],
    original_transfer_volume_cents: int,
) -> tuple[bool, str]:
    """
    Ensures per-person settlement impact matches net balances exactly. This
    per-person check is the actual correctness guarantee: if every person's
    total paid-out/received in the plan matches their true net balance, the
    plan is valid, full stop.

    We also sanity-check that the plan didn't move MORE money than the naive
    per-expense total (original_transfer_volume_cents). We deliberately do
    NOT require equality here: debt-simplification's whole point is to net
    balances across expenses so LESS money needs to move (e.g. someone who
    is owed money on one expense but owes money on another settles with a
    smaller/absent transfer instead of two full-size ones). Requiring
    equality would reject every correctly-simplified plan involving more
    than one expense with overlapping participants.
    """
    if sum(balances.values()) != 0:
        return False, "Net balances did not sum to zero."

    reconstructed = {name: 0 for name in balances.keys()}
    settlement_total_cents = 0

    for item in settlement_plan:
        payer = normalize_name(item.get("from", ""))
        receiver = normalize_name(item.get("to", ""))
        amount_cents = to_cents(item.get("amount", 0))

        if not payer or not receiver or payer == receiver:
            return False, "Settlement entry had invalid names."
        if amount_cents <= 0:
            return False, "Settlement entry had a non-positive amount."

        reconstructed.setdefault(payer, 0)
        reconstructed.setdefault(receiver, 0)

        reconstructed[payer] -= amount_cents
        reconstructed[receiver] += amount_cents
        settlement_total_cents += amount_cents

    for name, expected in balances.items():
        actual = reconstructed.get(name, 0)
        if actual != expected:
            return False, f"Settlement mismatch for {name}: expected {expected}, got {actual}."

    if settlement_total_cents > original_transfer_volume_cents:
        return False, (
            "Settlement moved more money than the original expenses required: "
            f"original volume {original_transfer_volume_cents}, settlement total {settlement_total_cents}."
        )

    return True, ""


def parse_expenses_step(user_text: str) -> dict:
    last_error = None
    parsed = None

    for attempt in range(2):
        try:
            raw_response = call_llm(PARSE_SYSTEM_PROMPT, build_parse_user_prompt(user_text))
        except UpstreamError:
            raise

        try:
            parsed = extract_json(raw_response)
        except json.JSONDecodeError as e:
            last_error = e
            logger.warning("Step 1 JSON parse failed on attempt %d: %s", attempt + 1, e)
            continue

        shape_error = validate_parse_shape(parsed)
        if shape_error:
            last_error = shape_error
            logger.warning("Step 1 shape validation failed on attempt %d: %s", attempt + 1, shape_error)
            parsed = None
            continue

        break

    if parsed is None:
        raise UpstreamError(
            "The AI parser response couldn't be understood, even after a retry. Please rephrase and try again.",
            502,
        )

    return parsed


def draft_reminders_step(settlement_plan: list[dict]) -> list[str]:
    if not settlement_plan:
        return []

    parsed = None
    last_error = None

    for attempt in range(2):
        try:
            raw_response = call_llm(REMINDER_SYSTEM_PROMPT, build_reminder_user_prompt(settlement_plan))
        except UpstreamError:
            raise

        try:
            parsed = extract_json(raw_response)
        except json.JSONDecodeError as e:
            last_error = e
            logger.warning("Step 4 JSON parse failed on attempt %d: %s", attempt + 1, e)
            continue

        shape_error = validate_reminder_shape(parsed, len(settlement_plan))
        if shape_error:
            last_error = shape_error
            logger.warning("Step 4 shape validation failed on attempt %d: %s", attempt + 1, shape_error)
            parsed = None
            continue

        break

    if parsed is None:
        logger.warning("Step 4 reminder generation failed after retry: %s", last_error)
        raise UpstreamError(
            "The reminder text couldn't be generated cleanly after a retry. Please try again.",
            502,
        )

    return parsed.get("reminders", [])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
from flask import redirect, url_for, flash


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.")
            return redirect(url_for("signup"))

        if User.query.filter_by(username=username).first():
            flash("That username is already taken.")
            return redirect(url_for("signup"))

        new_user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()

        login_user(new_user)
        return redirect(url_for("index"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid username or password.")
            return redirect(url_for("login"))

        login_user(user)
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/settle", methods=["POST"])
@login_required
def settle():
    payload = request.get_json(silent=True) or {}
    user_text = (payload.get("text") or "").strip()
    group_id = payload.get("group_id")

    # --- 1. Fast local validation (no API call wasted on junk input) ---
    validation_error = looks_like_gibberish_or_empty(user_text)
    if validation_error:
        return jsonify({"ok": False, "error": validation_error}), 400

    # --- 2. Make sure we actually have an API key configured ---
    if not API_KEY:
        logger.error("OPENROUTER_API_KEY is not set.")
        return jsonify({
            "ok": False,
            "error": "The server isn't configured with an OpenRouter API key. Add OPENROUTER_API_KEY to your .env file and restart the server."
        }), 500

    try:
        logger.info("Step 1: Parsing expenses...")
        parsed = parse_expenses_step(user_text)
        logger.info("Step 1 complete: Parsed %d expenses.", len(parsed.get("expense_summary", [])))
    except UpstreamError as e:
        return jsonify({"ok": False, "error": e.message}), e.status_code
    except Exception:
        logger.exception("Unexpected error in Step 1")
        return jsonify({"ok": False, "error": "Something went wrong while parsing expenses. Please try again."}), 500

    if "error" in parsed:
        return jsonify({"ok": False, "error": parsed["error"]}), 400

    expenses = parsed.get("expense_summary", [])

    settlement_plan = []
    balances_cents = {}
    original_transfer_volume_cents = 0
    verification_ok = False
    verification_error = ""

    logger.info("Step 2: Calculating settlement...")
    for attempt in range(2):
        try:
            balances_cents, original_transfer_volume_cents = compute_balances(expenses)
            settlement_plan = compute_settlement(balances_cents)
            logger.info("Step 2 complete (attempt %d): Settlement plan has %d transactions.", attempt + 1, len(settlement_plan))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception:
            logger.exception("Unexpected error in Step 2")
            return jsonify({"ok": False, "error": "Something went wrong while calculating the settlement plan."}), 500

        logger.info("Step 3: Verifying balances...")
        verification_ok, verification_error = verify_settlement(
            balances_cents,
            settlement_plan,
            original_transfer_volume_cents,
        )

        if verification_ok:
            logger.info("Step 3 complete: Verification passed on attempt %d.", attempt + 1)
            break

        logger.warning("Step 3 verification failed on attempt %d: %s", attempt + 1, verification_error)

    if not verification_ok:
        return jsonify({
            "ok": False,
            "error": "Internal settlement verification failed after retry. Please rephrase the expenses and try again."
        }), 500

    balances_list = [
        {"name": name, "net_balance": cents_to_amount(cents)}
        for name, cents in sorted(balances_cents.items(), key=lambda item: item[0].lower())
    ]

    logger.info("Step 4: Drafting reminders...")
    try:
        reminders = draft_reminders_step(settlement_plan)
        logger.info("Step 4 complete: Drafted %d reminders.", len(reminders))
    except UpstreamError as e:
        return jsonify({"ok": False, "error": e.message}), e.status_code
    except Exception:
        logger.exception("Unexpected error in Step 4")
        return jsonify({"ok": False, "error": "Something went wrong while drafting reminders. Please try again."}), 500
    for exp in expenses:
        new_expense = Expense(
            user_id=current_user.id,
            group_id=group_id,
            description=exp.get("description", ""),
            category=exp.get("category", "Other"),
            amount=exp.get("amount", 0),
            paid_by=exp.get("paid_by", ""),
            split_with=", ".join(exp.get("split_between", [])),
        )
        db.session.add(new_expense)
    db.session.commit()

    transaction_ids = []
    for item in settlement_plan:
        new_txn = Transaction(
            user_id=current_user.id,
            group_id=group_id,
            payer=item["from"],
            receiver=item["to"],
            amount=item["amount"],
        )
        db.session.add(new_txn)
        db.session.flush()  
        transaction_ids.append(new_txn.id)
    db.session.commit()

    for i, item in enumerate(settlement_plan):
        item["transaction_id"] = transaction_ids[i]

    return jsonify({
        "ok": True,
        "expense_summary": expenses,
        "balances": balances_list,
        "settlement_plan": settlement_plan,
        "reminders": reminders,
    })

from datetime import date, timedelta, datetime

EXPENSE_CATEGORIES = ["Food", "Travel", "Rent", "Utilities", "Entertainment", "Shopping", "Other"]


@app.route("/weekly")
@login_required
def weekly_summary():
    seven_days_ago = date.today() - timedelta(days=7)
    selected_category = request.args.get("category", "").strip()
    search_query = request.args.get("search", "").strip()

    query = (
        Expense.query
        .filter(Expense.user_id == current_user.id)
        .filter(Expense.date >= seven_days_ago)
    )

    if selected_category and selected_category in EXPENSE_CATEGORIES:
        query = query.filter(Expense.category == selected_category)

    if search_query:
        query = query.filter(Expense.description.ilike(f"%{search_query}%"))

    expenses = query.order_by(Expense.date.desc()).all()

    total_spent = sum(e.amount for e in expenses)
    expense_count = len(expenses)

    daily_totals = {}
    category_totals = {}
    for e in expenses:
        day_str = e.date.strftime("%A, %d %b") if e.date else "Unknown date"
        daily_totals[day_str] = daily_totals.get(day_str, 0) + e.amount
        category_totals[e.category] = category_totals.get(e.category, 0) + e.amount

    return render_template(
        "weekly.html",
        expenses=expenses,
        total_spent=total_spent,
        expense_count=expense_count,
        daily_totals=daily_totals,
        category_totals=category_totals,
        categories=EXPENSE_CATEGORIES,
        selected_category=selected_category,
        search_query=search_query,
    )


from calendar import monthrange


@app.route("/monthly")
@login_required
def monthly_summary():
    today = date.today()
    month_start = today.replace(day=1)
    days_in_month = monthrange(today.year, today.month)[1]
    month_end = today.replace(day=days_in_month)

    selected_category = request.args.get("category", "").strip()
    search_query = request.args.get("search", "").strip()

    query = (
        Expense.query
        .filter(Expense.user_id == current_user.id)
        .filter(Expense.date >= month_start)
        .filter(Expense.date <= month_end)
    )

    if selected_category and selected_category in EXPENSE_CATEGORIES:
        query = query.filter(Expense.category == selected_category)

    if search_query:
        query = query.filter(Expense.description.ilike(f"%{search_query}%"))

    expenses = query.order_by(Expense.date.desc()).all()

    total_spent = sum(e.amount for e in expenses)
    expense_count = len(expenses)

    category_totals = {}
    weekly_totals = {}  

    for e in expenses:
        category_totals[e.category] = category_totals.get(e.category, 0) + e.amount

        if e.date:
            week_number = ((e.date.day - 1) // 7) + 1
            week_label = f"Week {week_number}"
            weekly_totals[week_label] = weekly_totals.get(week_label, 0) + e.amount

    
    max_weeks = ((days_in_month - 1) // 7) + 1
    ordered_weekly_totals = {
        f"Week {i}": weekly_totals.get(f"Week {i}", 0)
        for i in range(1, max_weeks + 1)
    }

    return render_template(
        "monthly.html",
        expenses=expenses,
        total_spent=total_spent,
        expense_count=expense_count,
        category_totals=category_totals,
        weekly_totals=ordered_weekly_totals,
        categories=EXPENSE_CATEGORIES,
        selected_category=selected_category,
        search_query=search_query,
        month_name=today.strftime("%B %Y"),
    )


@app.route("/heatmap")
@login_required
def spending_heatmap():
    today = date.today()
    one_year_ago = today - timedelta(days=364)

    expenses = (
        Expense.query
        .filter(Expense.user_id == current_user.id)
        .filter(Expense.date >= one_year_ago)
        .all()
    )

    daily_totals = {}
    for e in expenses:
        if e.date:
            day_str = e.date.isoformat()
            daily_totals[day_str] = daily_totals.get(day_str, 0) + e.amount

    max_day_total = max(daily_totals.values()) if daily_totals else 0

    return render_template(
        "heatmap.html",
        daily_totals=daily_totals,
        max_day_total=max_day_total,
        start_date=one_year_ago.isoformat(),
        end_date=today.isoformat(),
    )

@app.route("/api/mark-paid/<int:txn_id>", methods=["POST"])
@login_required
def mark_paid(txn_id):
    txn = Transaction.query.filter_by(id=txn_id, user_id=current_user.id).first()
    if not txn:
        return jsonify({"ok": False, "error": "Transaction not found."}), 404

    txn.is_paid = not txn.is_paid
    db.session.commit()

    return jsonify({"ok": True, "is_paid": txn.is_paid})


@app.route("/payments")
@login_required
def payments():
    all_transactions = (
        Transaction.query
        .filter_by(user_id=current_user.id)
        .order_by(Transaction.created_at.desc())
        .all()
    )

    pending = [t for t in all_transactions if not t.is_paid]
    paid = [t for t in all_transactions if t.is_paid]

    return render_template("payments.html", pending=pending, paid=paid)

@app.route("/api/expense/<int:expense_id>/delete", methods=["POST"])
@login_required
def delete_expense(expense_id):
    expense = Expense.query.filter_by(id=expense_id, user_id=current_user.id).first()
    if not expense:
        return jsonify({"ok": False, "error": "Expense not found."}), 404

    if expense.receipt_filename:
        receipt_path = os.path.join(app.config["UPLOAD_FOLDER"], expense.receipt_filename)
        if os.path.exists(receipt_path):
            os.remove(receipt_path)

    db.session.delete(expense)
    db.session.commit()

    return jsonify({"ok": True})


@app.route("/api/expense/<int:expense_id>/edit", methods=["POST"])
@login_required
def edit_expense(expense_id):
    expense = Expense.query.filter_by(id=expense_id, user_id=current_user.id).first()
    if not expense:
        return jsonify({"ok": False, "error": "Expense not found."}), 404

    payload = request.get_json(silent=True) or {}

    description = payload.get("description", "").strip()
    amount = payload.get("amount")
    category = payload.get("category", "Other")

    if not description:
        return jsonify({"ok": False, "error": "Description cannot be empty."}), 400

    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Amount must be a valid number greater than 0."}), 400

    if category not in EXPENSE_CATEGORIES:
        category = "Other"

    expense.description = description
    expense.amount = amount
    expense.category = category
    db.session.commit()

    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

GROUP_CATEGORIES = ["Trip", "Flat", "Office", "Friends", "Family"]
GROUP_EMOJIS = ["👥", "✈️", "🏠", "💼", "🎉", "👨‍👩‍👧‍👦"]


@app.route("/groups")
@login_required
def groups_list():
    memberships = GroupMember.query.filter_by(user_id=current_user.id).all()
    my_groups = [m.group for m in memberships]
    return render_template("groups.html", groups=my_groups, categories=GROUP_CATEGORIES, emojis=GROUP_EMOJIS)


@app.route("/groups/create", methods=["POST"])
@login_required
def create_group():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    category = request.form.get("category", "Friends")
    emoji = request.form.get("emoji", "👥")

    if not name:
        flash("Group name is required.")
        return redirect(url_for("groups_list"))

    new_group = Group(
        name=name,
        description=description,
        category=category if category in GROUP_CATEGORIES else "Friends",
        avatar_emoji=emoji,
        owner_id=current_user.id,
    )
    db.session.add(new_group)
    db.session.commit()

    owner_membership = GroupMember(group_id=new_group.id, user_id=current_user.id, role="owner")
    db.session.add(owner_membership)
    db.session.commit()

    return redirect(url_for("group_detail", group_id=new_group.id))


def get_membership_or_404(group_id):
    """Returns the current user's membership in this group, or None if not a member."""
    return GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()


@app.route("/groups/<int:group_id>")
@login_required
def group_detail(group_id):
    group = Group.query.get_or_404(group_id)
    membership = get_membership_or_404(group_id)

    if not membership:
        flash("You're not a member of this group.")
        return redirect(url_for("groups_list"))

    all_members = GroupMember.query.filter_by(group_id=group_id).all()
    member_names = [m.user.username for m in all_members]

    recent_expenses = (
        Expense.query
        .filter_by(group_id=group_id)
        .order_by(Expense.created_at.desc())
        .limit(20)
        .all()
    )

    recent_transactions = (
        Transaction.query
        .filter_by(group_id=group_id)
        .order_by(Transaction.created_at.desc())
        .limit(20)
        .all()
    )

    pending_requests = []
    if membership.role in ("owner", "admin"):
        pending_requests = JoinRequest.query.filter_by(group_id=group_id, status="pending").all()

    invite_url = url_for("join_group", token=group.invite_token, _external=True)

    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(invite_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="#1C2B33", back_color="#ECF1EE")

    qr_buffer = BytesIO()
    qr_img.save(qr_buffer, format="PNG")
    qr_base64 = base64.b64encode(qr_buffer.getvalue()).decode("utf-8")

    running_balances = compute_group_running_balances(group_id)

    return render_template(
        "group_detail.html",
        group=group,
        members=all_members,
        member_names=member_names,
        my_role=membership.role,
        invite_url=invite_url,
        qr_code_data=qr_base64,
        recent_expenses=recent_expenses,
        recent_transactions=recent_transactions,
        pending_requests=pending_requests,
        running_balances=running_balances,
    )

@app.route("/join/<token>")
@login_required
def join_group(token):
    group = Group.query.filter_by(invite_token=token).first()

    if not group:
        flash("That invite link is invalid or has expired.")
        return redirect(url_for("groups_list"))

    existing_membership = get_membership_or_404(group.id)
    if existing_membership:
        return redirect(url_for("group_detail", group_id=group.id))

    existing_request = JoinRequest.query.filter_by(
        group_id=group.id, user_id=current_user.id, status="pending"
    ).first()
    if existing_request:
        flash(f"Your request to join {group.name} is already pending approval.")
        return redirect(url_for("groups_list"))

    new_request = JoinRequest(group_id=group.id, user_id=current_user.id)
    db.session.add(new_request)
    db.session.commit()

    flash(f"Your request to join {group.name} has been sent for approval.")
    return redirect(url_for("groups_list"))


@app.route("/groups/<int:group_id>/approve-request/<int:request_id>", methods=["POST"])
@login_required
def approve_join_request(group_id, request_id):
    membership = get_membership_or_404(group_id)
    if not membership or membership.role not in ("owner", "admin"):
        return jsonify({"ok": False, "error": "You don't have permission to approve requests."}), 403

    join_req = JoinRequest.query.filter_by(id=request_id, group_id=group_id, status="pending").first()
    if not join_req:
        return jsonify({"ok": False, "error": "Request not found."}), 404

    already_member = GroupMember.query.filter_by(group_id=group_id, user_id=join_req.user_id).first()
    if not already_member:
        new_membership = GroupMember(group_id=group_id, user_id=join_req.user_id, role="member")
        db.session.add(new_membership)

    join_req.status = "approved"
    db.session.commit()

    return jsonify({"ok": True})


@app.route("/groups/<int:group_id>/reject-request/<int:request_id>", methods=["POST"])
@login_required
def reject_join_request(group_id, request_id):
    membership = get_membership_or_404(group_id)
    if not membership or membership.role not in ("owner", "admin"):
        return jsonify({"ok": False, "error": "You don't have permission to reject requests."}), 403

    join_req = JoinRequest.query.filter_by(id=request_id, group_id=group_id, status="pending").first()
    if not join_req:
        return jsonify({"ok": False, "error": "Request not found."}), 404

    join_req.status = "rejected"
    db.session.commit()

    return jsonify({"ok": True})


@app.route("/groups/<int:group_id>/remove-member/<int:member_id>", methods=["POST"])
@login_required
def remove_member(group_id, member_id):
    membership = get_membership_or_404(group_id)

    if not membership or membership.role not in ("owner", "admin"):
        return jsonify({"ok": False, "error": "You don't have permission to remove members."}), 403

    target = GroupMember.query.filter_by(id=member_id, group_id=group_id).first()
    if not target:
        return jsonify({"ok": False, "error": "Member not found."}), 404

    if target.role == "owner":
        return jsonify({"ok": False, "error": "Cannot remove the group owner."}), 400

    db.session.delete(target)
    db.session.commit()

    return jsonify({"ok": True})


@app.route("/groups/<int:group_id>/transfer-ownership/<int:member_id>", methods=["POST"])
@login_required
def transfer_ownership(group_id, member_id):
    membership = get_membership_or_404(group_id)

    if not membership or membership.role != "owner":
        return jsonify({"ok": False, "error": "Only the current owner can transfer ownership."}), 403

    new_owner = GroupMember.query.filter_by(id=member_id, group_id=group_id).first()
    if not new_owner:
        return jsonify({"ok": False, "error": "Member not found."}), 404

    membership.role = "admin"
    new_owner.role = "owner"

    group = Group.query.get(group_id)
    group.owner_id = new_owner.user_id

    db.session.commit()
    return jsonify({"ok": True})



@app.route("/api/upload-receipt/<int:expense_id>", methods=["POST"])
@login_required
def upload_receipt(expense_id):
    expense = Expense.query.filter_by(id=expense_id, user_id=current_user.id).first()
    if not expense:
        return jsonify({"ok": False, "error": "Expense not found."}), 404

    if "receipt" not in request.files:
        return jsonify({"ok": False, "error": "No file was sent."}), 400

    file = request.files["receipt"]

    if file.filename == "":
        return jsonify({"ok": False, "error": "No file was selected."}), 400

    if not allowed_receipt_file(file.filename):
        return jsonify({"ok": False, "error": "Only PNG, JPG, WEBP, or PDF files are allowed."}), 400

    original_ext = file.filename.rsplit(".", 1)[1].lower()
    unique_filename = f"{uuid.uuid4().hex}.{original_ext}"
    safe_filename = secure_filename(unique_filename)

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], safe_filename)
    file.save(filepath)

    if expense.receipt_filename:
        old_path = os.path.join(app.config["UPLOAD_FOLDER"], expense.receipt_filename)
        if os.path.exists(old_path):
            os.remove(old_path)

    expense.receipt_filename = safe_filename
    db.session.commit()

    return jsonify({"ok": True, "receipt_url": url_for("static", filename=f"receipts/{safe_filename}")})


@app.route("/api/export-pdf", methods=["POST"])
@login_required
def export_pdf():
    payload = request.get_json(silent=True) or {}
    settlement_plan = payload.get("settlement_plan", [])
    expense_summary = payload.get("expense_summary", [])

    if not settlement_plan and not expense_summary:
        return jsonify({"ok": False, "error": "Nothing to export."}), 400

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=30*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "SplitSenseTitle", parent=styles["Title"],
        fontSize=22, textColor=colors.HexColor("#2F7A63"), spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "SplitSenseSubtitle", parent=styles["Normal"],
        fontSize=10, textColor=colors.HexColor("#56666B"), spaceAfter=20,
    )
    section_style = ParagraphStyle(
        "SectionHeader", parent=styles["Heading2"],
        fontSize=13, textColor=colors.HexColor("#1C2B33"), spaceBefore=16, spaceAfter=8,
    )

    elements = []
    elements.append(Paragraph("SplitSense", title_style))
    elements.append(Paragraph(
        f"Settlement statement generated on {datetime.utcnow().strftime('%d %B %Y, %I:%M %p')} UTC",
        subtitle_style
    ))

    if expense_summary:
        elements.append(Paragraph("Expense Summary", section_style))
        table_data = [["Description", "Paid By", "Split Between", "Amount"]]
        for exp in expense_summary:
            table_data.append([
                exp.get("description", ""),
                exp.get("paid_by", ""),
                ", ".join(exp.get("split_between", [])),
                f"Rs. {float(exp.get('amount', 0)):.2f}",
            ])

        table = Table(table_data, colWidths=[130, 80, 160, 70])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2F7A63")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCE4DE")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#ECF1EE")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(table)

    if settlement_plan:
        elements.append(Paragraph("Settlement Plan", section_style))
        settle_data = [["From", "To", "Amount"]]
        for item in settlement_plan:
            settle_data.append([
                item.get("from", ""),
                item.get("to", ""),
                f"Rs. {float(item.get('amount', 0)):.2f}",
            ])

        settle_table = Table(settle_data, colWidths=[150, 150, 100])
        settle_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1C2B33")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCE4DE")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#ECF1EE")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(settle_table)

    elements.append(Spacer(1, 20))
    elements.append(Paragraph(
        "Generated automatically by SplitSense — an autonomous expense-settling agent.",
        ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#9AA6AA"))
    ))

    doc.build(elements)
    buffer.seek(0)

    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"splitsense_settlement_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf",
    )

@app.route("/about")
@login_required
def about():
    return render_template("about.html")


@app.route("/contact")
@login_required
def contact():
    return render_template("contact.html")


if __name__ == "__main__":
    
    app.run(debug=True, port=5000, load_dotenv=False)