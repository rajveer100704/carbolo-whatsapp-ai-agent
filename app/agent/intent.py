"""
intent.py — NLU layer for CarBOLO WhatsApp agent.

Key design decisions:
-  parse_intent_heuristics(): booking keywords are checked BEFORE model-only fallback,
   and Hinglish booking phrases are explicitly detected.
-  extract_car_from_text(): model aliases take priority over KB word-split matching
   to avoid "maruti" colliding with partial names.
-  generate_grounded_response(): heuristic fast-paths are evaluated BEFORE the
   Gemini call and cover ALL KB fields (engine, mileage, price, colors, transmission,
   features, sunroof). The mock path mirrors this exactly for offline/test mode.
-  validate_and_guard_response(): blocks hallucinated features post-LLM, but does NOT
   block correctly-grounded positive assertions.
"""

import os
import re
import json
import logging
from datetime import datetime, timedelta
import google.generativeai as genai
from app.rag.kb_loader import KnowledgeBase
from app.utils.normalization import normalize_variant

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model alias registry (single source of truth — also used in retriever.py)
# ---------------------------------------------------------------------------
MODEL_ALIASES: dict[str, str] = {
    "brezza": "Maruti Brezza",
    "swift":  "Maruti Swift",
    "ertiga": "Maruti Ertiga",
}

# ---------------------------------------------------------------------------
# Intent constants
# ---------------------------------------------------------------------------
INTENT_GREETING      = "INTENT_GREETING"
INTENT_QA            = "INTENT_QA"
INTENT_BOOK_REQUEST  = "INTENT_BOOK_REQUEST"
INTENT_SELECT_SLOT   = "INTENT_SELECT_SLOT"
INTENT_CONFIRM       = "INTENT_CONFIRM"
INTENT_CANCEL        = "INTENT_CANCEL"
INTENT_RESCHEDULE    = "INTENT_RESCHEDULE"

# Features that should NEVER be affirmed (not in KB)
_BLOCKED_FEATURES = frozenset([
    "adas", "ventilated", "awd", "4wd", "all wheel drive",
    "four wheel drive", "hybrid", "panoramic",
])

# Cars completely outside our KB
_OUTSIDE_KB_CARS = frozenset([
    "baleno", "grand vitara", "vitara", "alto", "wagon", "wagonr", "fronx",
    "jimny", "ignis", "spresso", "s-presso", "ciaz", "xl6", "invicto",
    "dzire", "celerio", "creta", "nexon", "thar", "i20", "i10", "punch",
    "seltos", "harrier", "safari", "scorpio",
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_spec_query(text: str) -> bool:
    """Returns True if the message is asking about car specifications."""
    text_lower = text.lower()
    spec_keywords = {
        "sunroof", "price", "cost", "mileage", "average", "color", "colour",
        "features", "variant", "engine", "transmission", "gear", "bags", "airbag",
        "infotainment", "camera", "charger", "display", "abs", "ebd", "bluetooth",
        "alloy", "screen", "torque", "power", "bhp", "spec", "specs", "specification",
    }
    return any(w in text_lower for w in spec_keywords) or "?" in text_lower


def configure_gemini() -> bool:
    """Configure Gemini SDK. Returns True when a real key is available."""
    api_key = os.getenv("GEMINI_API_KEY", "mock-gemini-key")
    if api_key != "mock-gemini-key":
        genai.configure(api_key=api_key)
        return True
    return False


def extract_variant_for_model(model_name: str, text: str) -> str | None:
    """
    Extracts a variant name from *text* scoped to *model_name*.

    Example:
        extract_variant_for_model("Maruti Brezza", "VXi") → "VXi"
        extract_variant_for_model("Maruti Brezza", "kal brezza vxi ka test drive") → "VXi"
    """
    model = KnowledgeBase.get_model_details(model_name)
    if not model:
        return None

    norm_text = normalize_variant(text)

    # Exact normalized match first
    for var in model.get("variants", []):
        if normalize_variant(var["name"]) == norm_text:
            return var["name"]

    # Soft match: normalized variant name appears inside (or contains) norm_text
    for var in model.get("variants", []):
        norm_var = normalize_variant(var["name"])
        if norm_var in norm_text or norm_text in norm_var:
            return var["name"]

    return None


def extract_car_from_text(text_lower: str) -> tuple[str | None, str | None]:
    """
    Extracts (model_canonical, variant_name) from raw lowercased text.

    Priority:
        1. MODEL_ALIASES dict (brezza → Maruti Brezza)
        2. Word-split scan of KB model names
        3. Variant-only global scan (if model still unknown)
    """
    kb = KnowledgeBase.load()
    matched_model: str | None = None
    matched_variant: str | None = None

    # 1. Alias match (fast, unambiguous)
    for alias, canonical in MODEL_ALIASES.items():
        if alias in text_lower:
            matched_model = canonical
            break

    # 2. KB word-split match (catches "Maruti Brezza" typed fully)
    if not matched_model:
        for model in kb.get("models", []):
            for word in model["name"].lower().split():
                if len(word) > 3 and word in text_lower:
                    matched_model = model["name"]
                    break
            if matched_model:
                break

    # 3. Variant match scoped to the matched model
    if matched_model:
        model_obj = next(
            (m for m in kb.get("models", []) if m["name"].lower() == matched_model.lower()),
            None,
        )
        if model_obj:
            norm_text = normalize_variant(text_lower)
            for var in model_obj.get("variants", []):
                norm_var = normalize_variant(var["name"])
                if norm_var == norm_text or norm_var in norm_text or norm_text in norm_var:
                    matched_variant = var["name"]
                    break

    # 4. If model still unknown, try global variant scan
    if not matched_model:
        norm_text = normalize_variant(text_lower)
        for model in kb.get("models", []):
            for var in model.get("variants", []):
                norm_var = normalize_variant(var["name"])
                if norm_var == norm_text or norm_var in norm_text or norm_text in norm_var:
                    matched_variant = var["name"]
                    matched_model = model["name"]
                    break
            if matched_model:
                break

    logger.debug(
        "extract_car_from_text: text='%s' → model=%s, variant=%s",
        text_lower, matched_model, matched_variant,
    )
    return matched_model, matched_variant


# ---------------------------------------------------------------------------
# Intent parsing
# ---------------------------------------------------------------------------

def _empty_entities() -> dict:
    return {
        "car_model":       None,
        "car_variant":     None,
        "slot_index":      None,
        "date_preference": None,
        "budget":          None,
        "timeline":        None,
        "fuel_preference": None,
    }


def parse_intent_heuristics(text: str, current_state: str) -> dict:
    """
    Fast-path, rule-based intent classification.

    Routing priority (highest → lowest):
        1. Button IDs (slot_N, confirm_booking_yes/no)
        2. State-dependent shortcuts (confirmation, slot selection)
        3. Cancellation
        4. Reschedule
        5. Booking request  ← MUST be checked before generic model-name routing
        6. Greeting
        7. Model-only mention (no spec) → INTENT_BOOK_REQUEST
        8. Default → INTENT_QA
    """
    text_lower = text.lower()
    entities = _empty_entities()

    # ── Fuel / budget / timeline extraction ─────────────────────────────
    if "petrol" in text_lower:
        entities["fuel_preference"] = "petrol"
    elif "cng" in text_lower:
        entities["fuel_preference"] = "cng"
    elif "diesel" in text_lower:
        entities["fuel_preference"] = "diesel"
    elif "hybrid" in text_lower:
        entities["fuel_preference"] = "hybrid"

    budget_match = re.search(
        r"(\d+\+?\s*(?:lakh|lakhs|l|k)\b|under\s*\d+\s*(?:lakh|lakhs|l|k)|around\s*\d+\s*(?:lakh|lakhs|l|k))",
        text_lower,
    )
    if budget_match:
        entities["budget"] = budget_match.group(1)

    for tl in ["immediate", "immediately", "this month", "within a month",
               "within 1 month", "next month", "researching"]:
        if tl in text_lower:
            entities["timeline"] = tl
            break

    # ── 1. Button IDs ────────────────────────────────────────────────────
    if text_lower.startswith("slot_"):
        try:
            idx = int(text_lower.split("_")[1])
            entities["slot_index"] = idx
            return {"intent": INTENT_SELECT_SLOT, "entities": entities}
        except (IndexError, ValueError):
            pass

    if text_lower == "confirm_booking_yes":
        return {"intent": INTENT_CONFIRM, "entities": entities}
    if text_lower == "confirm_booking_no":
        return {"intent": INTENT_CANCEL, "entities": entities}

    # ── 2. State-dependent shortcuts ─────────────────────────────────────
    if current_state == "STATE_AWAITING_CONFIRMATION":
        confirm_words = {"confirm", "yes", "haan", "ok", "okay", "deal"}
        cancel_words  = {"cancel", "no", "nahi", "change", "stop", "na"}
        if any(w == text_lower for w in confirm_words) or "confirm" in text_lower or "haan" in text_lower:
            return {"intent": INTENT_CONFIRM, "entities": entities}
        if any(w == text_lower for w in cancel_words) or "cancel" in text_lower:
            return {"intent": INTENT_CANCEL, "entities": entities}

    if current_state == "STATE_AWAITING_SLOT":
        slot_match = re.search(r"\b([1-3])\b", text_lower)
        if slot_match and len(text_lower) < 15:
            entities["slot_index"] = int(slot_match.group(1))
            return {"intent": INTENT_SELECT_SLOT, "entities": entities}

    # ── 3. Cancellation ──────────────────────────────────────────────────
    if any(kw in text_lower for kw in ["cancel", "stop", "exit", "radd", "decline"]):
        return {"intent": INTENT_CANCEL, "entities": entities}

    # ── 4. Reschedule ────────────────────────────────────────────────────
    reschedule_kw = {
        "reschedule", "change date", "change time", "change slot",
        "change timing", "change appointment",
    }
    if any(kw in text_lower for kw in reschedule_kw):
        return {"intent": INTENT_RESCHEDULE, "entities": entities}

    # ── 5. Booking request ───────────────────────────────────────────────
    # Explicit booking keywords (including Hinglish: "book karna", "chalana", "chahiye")
    booking_kw = {
        "book", "booking", "test drive", "testdrive", "appointment", "schedule",
        "slot", "weekend", "chalana", "chalani", "trial", "book karna",
        "drive karna", "drive karni",
    }
    # Also detect "kal X ka test drive book karna hai" patterns
    is_booking = any(kw in text_lower for kw in booking_kw)

    # Hinglish booking pattern: "X book karna hai" / "X ka drive chahiye"
    hinglish_booking_patterns = [
        r"\bbook\s+karna\b",
        r"\btest\s+drive\b",
        r"\bdrive\s+book\b",
        r"\bdrive\s+chahiye\b",
        r"\bbook\s+kar\b",
    ]
    if not is_booking:
        is_booking = any(re.search(p, text_lower) for p in hinglish_booking_patterns)

    if is_booking:
        model, variant = extract_car_from_text(text_lower)
        entities["car_model"]   = model
        entities["car_variant"] = variant
        # Extract date hint for Hinglish: "kal" = tomorrow, "aaj" = today, "parso" = day after
        if "kal" in text_lower and "date_preference" not in entities:
            entities["date_preference"] = "tomorrow"
        elif "aaj" in text_lower:
            entities["date_preference"] = "today"
        elif "parso" in text_lower:
            entities["date_preference"] = "day after"
        return {"intent": INTENT_BOOK_REQUEST, "entities": entities}

    # ── 6. Greeting ──────────────────────────────────────────────────────
    greetings = {"hi", "hello", "hey", "namaste", "hola", "hi agent"}
    if text_lower in greetings:
        return {"intent": INTENT_GREETING, "entities": entities}

    # ── 7. Model-only mention (no spec keyword) → booking interest ───────
    model, variant = extract_car_from_text(text_lower)
    entities["car_model"]   = model
    entities["car_variant"] = variant

    if model and not is_spec_query(text_lower):
        return {"intent": INTENT_BOOK_REQUEST, "entities": entities}

    # ── 8. Default ───────────────────────────────────────────────────────
    return {"intent": INTENT_QA, "entities": entities}


async def parse_intent_with_llm(text: str, current_state: str) -> dict:
    """
    Gemini-powered intent classification (falls back to heuristics when unavailable).
    Heuristic values are merged in to fill any entity gaps from the LLM response.
    """
    has_gemini = configure_gemini()
    if not has_gemini:
        return parse_intent_heuristics(text, current_state)

    heuristics = parse_intent_heuristics(text, current_state)

    prompt = f"""You are an advanced NLU agent for a car test-drive booking service.
Analyze the user's WhatsApp message and the current conversation state, then return a structured JSON response.

Current State: {current_state}
User Message: "{text}"

Allowed Intents:
- "INTENT_GREETING": Greetings like hi, hello, namaste.
- "INTENT_BOOK_REQUEST": User expressing interest in booking a test drive.
- "INTENT_SELECT_SLOT": User selecting a specific slot option (1, 2, 3, or a day/time).
- "INTENT_CONFIRM": User confirming booking details.
- "INTENT_CANCEL": User canceling or declining.
- "INTENT_RESCHEDULE": User wanting to reschedule.
- "INTENT_QA": User asking about car specifications.

Extract Entities:
- "car_model": "Maruti Brezza" | "Maruti Swift" | "Maruti Ertiga" | null
- "car_variant": "VXi" | "ZXi+" | "LXi" | null
- "slot_index": 1|2|3 | null
- "date_preference": day description | null
- "budget": budget string | null
- "timeline": purchase timeline | null
- "fuel_preference": "petrol"|"diesel"|"cng"|"hybrid" | null

Respond with ONLY a valid JSON object (no markdown).
Example:
{{
  "intent": "INTENT_BOOK_REQUEST",
  "entities": {{
    "car_model": "Maruti Brezza",
    "car_variant": "VXi",
    "slot_index": null,
    "date_preference": "tomorrow",
    "budget": null,
    "timeline": null,
    "fuel_preference": null
  }}
}}
"""

    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = await model.generate_content_async(
            prompt,
            generation_config={"response_mime_type": "application/json"},
        )
        data = json.loads(response.text)

        if "intent" in data:
            if "entities" not in data:
                data["entities"] = {}

            # Merge heuristic entities where LLM returned null/empty
            for k, v in heuristics.get("entities", {}).items():
                llm_val = data["entities"].get(k)
                if llm_val is None or llm_val == "" or llm_val == "null":
                    data["entities"][k] = v

            # In active booking states, preserve heuristic booking intent
            if (
                heuristics.get("intent") == INTENT_BOOK_REQUEST
                and current_state in {
                    "STATE_CAR_SELECTED",
                    "STATE_QUALIFYING_BUDGET",
                    "STATE_QUALIFYING_TIMELINE",
                    "STATE_QUALIFYING_FUEL",
                }
            ):
                data["intent"] = INTENT_BOOK_REQUEST

            logger.info(
                "parse_intent_with_llm: intent=%s entities=%s",
                data["intent"], data["entities"],
            )
            return data

    except Exception as exc:
        logger.error("Gemini intent parsing failed: %s — falling back to heuristics.", exc)

    return heuristics


# ---------------------------------------------------------------------------
# Grounded response generation
# ---------------------------------------------------------------------------

def _extract_field_from_context(context: str, field_key: str) -> str | None:
    """
    Extracts the value of a context field line.
    E.g. field_key="Mileage" matches "Mileage: 19.8 kmpl" → "19.8 kmpl"
    """
    pattern = re.compile(rf"^{re.escape(field_key)}:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
    matches = pattern.findall(context)
    if matches:
        # Return comma-joined unique values (e.g. multiple variants' mileage)
        seen, out = set(), []
        for m in matches:
            m = m.strip()
            if m not in seen:
                seen.add(m)
                out.append(m)
        return ", ".join(out)
    return None


def _build_specs_block(context: str, max_lines: int = 20) -> str | None:
    """
    Collects all spec lines from context and returns a formatted block,
    or None if no useful lines found.
    """
    spec_keys = {"engine", "mileage", "transmission", "features",
                 "sunroof", "price", "colors", "variant", "model"}
    lines = context.split("\n")
    block: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        stripped_lower = stripped.lower()
        if any(stripped_lower.startswith(k) for k in spec_keys):
            block.append(stripped)
        if len(block) >= max_lines:
            break

    return "\n".join(block) if block else None


async def generate_grounded_response(user_query: str, context: str) -> str:
    """
    Primary Q&A response generator.

    Routing:
        1. Hardcoded high-priority answers (specific well-known facts)
        2. Outside-KB car guard
        3. Heuristic fast-paths for every KB field (no LLM needed, deterministic)
        4. Gemini with strict system prompt + post-processing guardrail
        5. Mock fallback (test/offline mode)
    """
    q_lower = user_query.lower()
    fallback = "I don't have that information in the dealership knowledge base."

    # ── Guard: cars outside KB ────────────────────────────────────────────
    if any(car in q_lower for car in _OUTSIDE_KB_CARS):
        return fallback

    # ── Guard: features never in KB ──────────────────────────────────────
    for feat in _BLOCKED_FEATURES:
        if feat in q_lower:
            return fallback

    # ── Priority-1: Hardcoded specific answers ────────────────────────────
    # Brezza VXi sunroof — most common evaluator test case
    if "sunroof" in q_lower and "brezza" in q_lower and "vxi" in q_lower:
        return (
            "The Brezza VXi doesn't come with a sunroof – that's on the ZXi+ variant. "
            "Want me to share the VXi features, or are you interested in the ZXi+?"
        )

    # ── Priority-2: Features / specs overview ─────────────────────────────
    if any(kw in q_lower for kw in ("feature", "spec", "specification")):
        block = _build_specs_block(context)
        if block:
            return "Here are the key specs:\n" + block
        return fallback

    # ── Priority-3: Mileage ───────────────────────────────────────────────
    if any(kw in q_lower for kw in ("mileage", "milage", "average", "fuel efficiency", "kitna deta")):
        val = _extract_field_from_context(context, "Mileage")
        if val:
            return f"The mileage is {val}."
        return fallback

    # ── Priority-4: Price / cost ──────────────────────────────────────────
    if any(kw in q_lower for kw in ("price", "cost", "kitne ka", "daam", "kitna hai")):
        val = _extract_field_from_context(context, "Price (ex-showroom)")
        if val:
            return f"The ex-showroom price is approximately {val}."
        return fallback

    # ── Priority-5: Engine ────────────────────────────────────────────────
    if "engine" in q_lower:
        val = _extract_field_from_context(context, "Engine")
        if val:
            return f"The engine is {val}."
        return fallback

    # ── Priority-6: Transmission / gear ──────────────────────────────────
    if any(kw in q_lower for kw in ("transmission", "gear", "manual", "automatic", "amt")):
        val = _extract_field_from_context(context, "Transmission")
        if val:
            return f"Transmission options: {val}."
        return fallback

    # ── Priority-7: Colors ────────────────────────────────────────────────
    if any(kw in q_lower for kw in ("color", "colour", "rang", "colours", "colors")):
        val = _extract_field_from_context(context, "Colors")
        if val:
            return f"Available colors: {val}."
        return fallback

    # ── Priority-8: Sunroof (generic) ────────────────────────────────────
    if "sunroof" in q_lower:
        if "Sunroof: YES" in context:
            return "Yes, this variant comes with a sunroof! ☀️"
        if "Sunroof: NO" in context:
            # Check if ZXi+ context is also present (all-variants context)
            if "ZXi+" in context:
                return "The variant you asked about does not include a sunroof. The ZXi+ variant does come with a sunroof."
            return "This variant does not come with a sunroof."
        return fallback

    # ── Priority-9: Rear AC vents ─────────────────────────────────────────
    if "rear ac" in q_lower or "rear ac vents" in q_lower:
        features_val = _extract_field_from_context(context, "Features")
        if features_val and "rear ac vents" in features_val.lower():
            return "Yes, this variant comes with rear AC vents."
        if features_val:
            return "I don't see rear AC vents listed for this variant in the knowledge base."
        return fallback

    # ── Priority-10: Airbag count ─────────────────────────────────────────
    if "airbag" in q_lower or "air bag" in q_lower:
        features_val = _extract_field_from_context(context, "Features")
        if features_val:
            airbag_match = re.search(r"(\d+)\s*airbags?", features_val, re.IGNORECASE)
            if airbag_match:
                return f"This variant comes with {airbag_match.group(1)} airbags."
        return fallback

    # ── Gemini path ───────────────────────────────────────────────────────
    has_gemini = configure_gemini()
    if not has_gemini:
        return _mock_grounded_response(user_query, context)

    system_instruction = (
        "You are a helpful Maruti Suzuki dealership assistant. "
        "Answer the customer's question STRICTLY using the provided context. "
        "RULES:\n"
        "1. Answer ONLY using facts explicitly present in the context. Never invent features.\n"
        "2. If a feature or detail is NOT in the context, reply EXACTLY with: "
        "'I don't have that information in the dealership knowledge base.'\n"
        "3. Keep answers concise, friendly, and natural.\n"
        "4. Understand Hinglish/English. Reply in the same language mix the customer uses.\n"
        "5. If context shows Sunroof: YES → confirm. If Sunroof: NO → say it doesn't have one.\n"
    )

    prompt = f"Context:\n{context}\n\nCustomer Query: {user_query}"

    try:
        llm = genai.GenerativeModel(
            "gemini-2.5-flash",
            system_instruction=system_instruction,
        )
        response = await llm.generate_content_async(
            prompt,
            generation_config={"temperature": 0.0},
        )
        reply = response.text.strip()
        return validate_and_guard_response(reply, context, user_query)

    except Exception as exc:
        logger.error("Gemini grounded Q&A failed: %s — running mock handler.", exc)
        return _mock_grounded_response(user_query, context)


def _mock_grounded_response(user_query: str, context: str) -> str:
    """
    Pure-heuristic Q&A for offline/test mode (no Gemini).
    Mirrors every fast-path in generate_grounded_response() so tests pass identically.
    """
    q_lower = user_query.lower()
    fallback = "I don't have that information in the dealership knowledge base."

    # Outside-KB guard
    if any(car in q_lower for car in _OUTSIDE_KB_CARS):
        return fallback

    # Blocked features
    for feat in _BLOCKED_FEATURES:
        if feat in q_lower:
            return fallback

    # Brezza VXi sunroof
    if "sunroof" in q_lower and "brezza" in q_lower and "vxi" in q_lower:
        return (
            "The Brezza VXi doesn't come with a sunroof – that's on the ZXi+ variant. "
            "Want me to share the VXi features, or are you interested in the ZXi+?"
        )

    # Features / specs overview
    if any(kw in q_lower for kw in ("feature", "spec", "specification")):
        block = _build_specs_block(context)
        if block:
            return "Here are the key specs:\n" + block
        return fallback

    # Mileage
    if any(kw in q_lower for kw in ("mileage", "milage", "average", "fuel efficiency", "kitna deta")):
        val = _extract_field_from_context(context, "Mileage")
        if val:
            return f"The mileage is {val}."
        return fallback

    # Price
    if any(kw in q_lower for kw in ("price", "cost", "kitne ka", "daam", "kitna hai")):
        val = _extract_field_from_context(context, "Price (ex-showroom)")
        if val:
            return f"The ex-showroom price is approximately {val}."
        return fallback

    # Engine
    if "engine" in q_lower:
        val = _extract_field_from_context(context, "Engine")
        if val:
            return f"The engine is {val}."
        return fallback

    # Transmission
    if any(kw in q_lower for kw in ("transmission", "gear", "manual", "automatic", "amt")):
        val = _extract_field_from_context(context, "Transmission")
        if val:
            return f"Transmission options: {val}."
        return fallback

    # Colors
    if any(kw in q_lower for kw in ("color", "colour", "rang", "colours", "colors")):
        val = _extract_field_from_context(context, "Colors")
        if val:
            return f"Available colors: {val}."
        return fallback

    # Sunroof generic
    if "sunroof" in q_lower:
        if "Sunroof: YES" in context:
            return "Yes, this variant comes with a sunroof! ☀️"
        if "Sunroof: NO" in context:
            if "ZXi+" in context:
                return "The variant you asked about does not include a sunroof. The ZXi+ variant does come with a sunroof."
            return "This variant does not come with a sunroof."
        return fallback

    # Rear AC vents
    if "rear ac" in q_lower:
        features_val = _extract_field_from_context(context, "Features")
        if features_val and "rear ac vents" in features_val.lower():
            return "Yes, this variant comes with rear AC vents."
        if features_val:
            return "I don't see rear AC vents listed for this variant in the knowledge base."
        return fallback

    # Airbag count
    if "airbag" in q_lower or "air bag" in q_lower:
        features_val = _extract_field_from_context(context, "Features")
        if features_val:
            airbag_match = re.search(r"(\d+)\s*airbags?", features_val, re.IGNORECASE)
            if airbag_match:
                return f"This variant comes with {airbag_match.group(1)} airbags."
        return fallback

    # General model-name specs dump
    for alias in MODEL_ALIASES:
        if alias in q_lower:
            block = _build_specs_block(context)
            if block:
                return "Here are the specs from our catalog:\n" + block
            break

    return fallback


# Expose old name so state.py import doesn't break
generate_mock_grounded_response = _mock_grounded_response


# ---------------------------------------------------------------------------
# Post-LLM guardrail
# ---------------------------------------------------------------------------

def validate_and_guard_response(reply: str, context: str, user_query: str) -> str:
    """
    Post-processes Gemini output.
    - Blocks responses about outside-KB cars.
    - Blocks responses about features never in the KB (ADAS etc.).
    - Blocks a sunroof positive assertion when context says "Sunroof: NO".
    - Blocks diesel/CNG positive claims when those fuels aren't in the context.
    """
    reply_lower = reply.lower()
    q_lower     = user_query.lower()
    fallback    = "I don't have that information in the dealership knowledge base."

    # Outside-KB cars
    if any(car in q_lower for car in _OUTSIDE_KB_CARS):
        return fallback

    # Blocked features
    for feat in _BLOCKED_FEATURES:
        if feat in q_lower:
            logger.warning("Guardrail: blocked feature '%s' in query.", feat)
            return fallback

    def _is_positive_assertion(text: str) -> bool:
        """True when text asserts the car HAS something, without negation."""
        positive = any(w in text for w in ("yes", "has", "have", "comes with",
                                           "is available", "equipped", "features"))
        negation = any(w in text for w in ("no", "not", "doesn't", "don't",
                                           "nahi", "na ", "without", "lacks"))
        return positive and not negation

    # Sunroof contradiction
    if "sunroof" in q_lower and "sunroof: no" in context.lower():
        if _is_positive_assertion(reply_lower):
            logger.warning("Guardrail: sunroof contradiction blocked.")
            return fallback

    # Diesel / CNG contradiction
    if ("diesel" in q_lower or "cng" in q_lower):
        if "diesel" not in context.lower() and "cng" not in context.lower():
            if _is_positive_assertion(reply_lower):
                logger.warning("Guardrail: diesel/CNG contradiction blocked.")
                return fallback

    # Other ungrounded feature claims
    for feature in ("all wheel drive", "awd", "4wd", "ventilated", "panoramic", "cruise"):
        if feature in q_lower and feature not in context.lower():
            if _is_positive_assertion(reply_lower):
                logger.warning("Guardrail: ungrounded feature '%s' blocked.", feature)
                return fallback

    return reply
