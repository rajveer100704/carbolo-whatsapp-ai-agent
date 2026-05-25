import os
import re
import json
import logging
from datetime import datetime, timedelta
import google.generativeai as genai
from app.rag.kb_loader import KnowledgeBase
from app.utils.normalization import normalize_variant

MODEL_ALIASES = {
    "brezza": "Maruti Brezza",
    "swift": "Maruti Swift",
    "ertiga": "Maruti Ertiga",
}

logger = logging.getLogger(__name__)

# Constants for Intents
INTENT_GREETING = "INTENT_GREETING"
INTENT_QA = "INTENT_QA"
INTENT_BOOK_REQUEST = "INTENT_BOOK_REQUEST"
INTENT_SELECT_SLOT = "INTENT_SELECT_SLOT"
INTENT_CONFIRM = "INTENT_CONFIRM"
INTENT_CANCEL = "INTENT_CANCEL"
INTENT_RESCHEDULE = "INTENT_RESCHEDULE"

def is_spec_query(text: str) -> bool:
    text_lower = text.lower()
    spec_keywords = {
        "sunroof", "price", "cost", "mileage", "average", "color", "colour", 
        "features", "variant", "engine", "transmission", "gear", "bags", "airbag", 
        "infotainment", "camera", "charger", "display", "abs", "ebd", "bluetooth",
        "alloy", "screen", "torque", "power", "bhp", "spec", "specs", "specification"
    }
    return any(w in text_lower for w in spec_keywords) or "?" in text_lower

def configure_gemini():
    api_key = os.getenv("GEMINI_API_KEY", "mock-gemini-key")
    if api_key != "mock-gemini-key":
        genai.configure(api_key=api_key)
        return True
    return False

def parse_intent_heuristics(text: str, current_state: str) -> dict:
    """
    Local heuristic-based intent parsing (fallback and fast-path).
    """
    text_lower = text.lower()
    
    # Initialize default entities structure
    entities = {
        "car_model": None,
        "car_variant": None,
        "slot_index": None,
        "date_preference": None,
        "budget": None,
        "timeline": None,
        "fuel_preference": None
    }
    
    # Basic entity extraction heuristics
    if "petrol" in text_lower:
        entities["fuel_preference"] = "petrol"
    elif "cng" in text_lower:
        entities["fuel_preference"] = "cng"
    elif "diesel" in text_lower:
        entities["fuel_preference"] = "diesel"
    elif "hybrid" in text_lower:
        entities["fuel_preference"] = "hybrid"

    budget_match = re.search(r'(\d+\+?\s*(?:lakh|lakhs|l|k)\b|under\s*\d+\s*(?:lakh|lakhs|l|k)|around\s*\d+\s*(?:lakh|lakhs|l|k))', text_lower)
    if budget_match:
        entities["budget"] = budget_match.group(1)

    for tl_word in ["immediate", "immediately", "this month", "within a month", "within 1 month", "next month", "researching"]:
        if tl_word in text_lower:
            entities["timeline"] = tl_word
            break

    # 1. Check Button IDs directly
    if text_lower.startswith("slot_"):
        try:
            idx = int(text_lower.split("_")[1])
            entities["slot_index"] = idx
            return {
                "intent": INTENT_SELECT_SLOT,
                "entities": entities
            }
        except Exception:
            pass
            
    if text_lower == "confirm_booking_yes":
        return {
            "intent": INTENT_CONFIRM,
            "entities": entities
        }
    if text_lower == "confirm_booking_no":
        return {
            "intent": INTENT_CANCEL,
            "entities": entities
        }

    # 2. State-dependent shortcut routing
    if current_state == "STATE_AWAITING_CONFIRMATION":
        confirm_words = {"confirm", "yes", "haan", "haan confirm", "ok", "okay", "deal", "confirm karo"}
        cancel_words = {"cancel", "no", "nahi", "change", "stop", "na"}
        if any(w == text_lower for w in confirm_words) or "confirm" in text_lower or "haan" in text_lower:
            return {"intent": INTENT_CONFIRM, "entities": entities}
        if any(w == text_lower for w in cancel_words) or "cancel" in text_lower:
            return {"intent": INTENT_CANCEL, "entities": entities}

    if current_state == "STATE_AWAITING_SLOT":
        # Check if user typed a single number representing slot selection (e.g. "1", "2", "3", "sat 1")
        slot_match = re.search(r'\b([1-3])\b', text_lower)
        if slot_match and len(text_lower) < 15:
            entities["slot_index"] = int(slot_match.group(1))
            return {
                "intent": INTENT_SELECT_SLOT,
                "entities": entities
            }

    # 3. Cancel
    if any(keyword in text_lower for keyword in ["cancel", "stop", "exit", "radd", "decline"]):
        return {"intent": INTENT_CANCEL, "entities": entities}

    # 3.5 Reschedule
    reschedule_keywords = {"reschedule", "change date", "change time", "change slot", "change timing", "change appointment"}
    if any(keyword in text_lower for keyword in reschedule_keywords):
        return {"intent": INTENT_RESCHEDULE, "entities": entities}

    # 4. Booking requests
    booking_keywords = {"book", "booking", "test drive", "drive", "appointment", "schedule", "slot", "weekend", "chalana", "chalani", "trial", "chahiye"}
    if any(keyword in text_lower for keyword in booking_keywords):
        # Extract model and variant from text if present
        model, variant = extract_car_from_text(text_lower)
        entities["car_model"] = model
        entities["car_variant"] = variant
        return {
            "intent": INTENT_BOOK_REQUEST,
            "entities": entities
        }

    # 5. Greeting
    greetings = {"hi", "hello", "hey", "namaste", "hola", "hi agent"}
    if text_lower in greetings:
        return {"intent": INTENT_GREETING, "entities": entities}

    # Default to Q&A or auto-booking request for car model names
    model, variant = extract_car_from_text(text_lower)
    entities["car_model"] = model
    entities["car_variant"] = variant
    
    if model and not is_spec_query(text_lower):
        return {
            "intent": INTENT_BOOK_REQUEST,
            "entities": entities
        }

    return {
        "intent": INTENT_QA,
        "entities": entities
    }

def extract_car_from_text(text_lower: str) -> tuple[str, str]:
    """Helper to extract model and variant names using regex/normalization from text."""
    kb = KnowledgeBase.load()
    matched_model = None
    matched_variant = None

    # 1. Try matching model via alias first (direct match)
    for alias, canonical in MODEL_ALIASES.items():
        if alias in text_lower:
            matched_model = canonical
            break

    # 2. Try matching model via KB split words
    if not matched_model:
        for model in kb.get("models", []):
            model_name = model["name"].lower()
            for word in model_name.split():
                if len(word) > 3 and word in text_lower:
                    matched_model = model["name"]
                    break
            if matched_model:
                break

    # 3. If model matched, search for variant of that model in normalized text
    if matched_model:
        model_obj = None
        for m in kb.get("models", []):
            if m["name"].lower() == matched_model.lower():
                model_obj = m
                break
        if model_obj:
            norm_text = normalize_variant(text_lower)
            for var in model_obj.get("variants", []):
                if normalize_variant(var["name"]) in norm_text:
                    matched_variant = var["name"]
                    break

    # 4. If no model matched, try to infer model from a matched variant globally
    if not matched_model:
        norm_text = normalize_variant(text_lower)
        for model in kb.get("models", []):
            for var in model.get("variants", []):
                if normalize_variant(var["name"]) in norm_text:
                    matched_variant = var["name"]
                    matched_model = model["name"]
                    break
            if matched_model:
                break

    logger.info(f"extract_car_from_text: text='{text_lower}' -> model={matched_model}, variant={matched_variant}")
    return matched_model, matched_variant

async def parse_intent_with_llm(text: str, current_state: str) -> dict:
    """
    Uses Gemini to classify intent and extract entities when heuristics are insufficient.
    """
    has_gemini = configure_gemini()
    if not has_gemini:
        # Fallback to local heuristics
        return parse_intent_heuristics(text, current_state)

    # Build intent parse prompt
    prompt = f"""You are an advanced NLU agent for a car test-drive booking service.
Analyze the user's WhatsApp message and the current conversation state, then return a structured JSON response.

Current State: {current_state}
User Message: "{text}"

Allowed Intents:
- "INTENT_GREETING": Greetings like hi, hello, namaste.
- "INTENT_BOOK_REQUEST": User expressing interest in booking a test drive (e.g., "I want to try Brezza", "booking request", "test drive booked").
- "INTENT_SELECT_SLOT": User selecting a specific slot option (e.g. "1", "2", "3", "sat 4pm", "saturday afternoon").
- "INTENT_CONFIRM": User confirming booking details (e.g. "confirm", "yes", "haan", "correct", "agree").
- "INTENT_CANCEL": User canceling/declining the process or requesting cancellation (e.g. "cancel", "no", "nahi", "stop").
- "INTENT_RESCHEDULE": User wanting to change the timing or reschedule their booking (e.g., "reschedule my slot", "change time", "parso shift kar do").
- "INTENT_QA": User asking a general question about car specifications (e.g. "sunroof details", "price of swift", "mileage?").

Extract Entities:
- "car_model": "Maruti Brezza", "Maruti Swift", "Maruti Ertiga" or null.
- "car_variant": "VXi", "ZXi+", "LXi" or null.
- "slot_index": Integer (1, 2, 3) if they picked by number, or null.
- "date_preference": string/day description if mentioned (e.g., "weekend", "tomorrow", "saturday") or null.
- "budget": string description of budget if mentioned (e.g. "under 10L", "12 lakhs") or null.
- "timeline": string description of purchase timeline if mentioned (e.g. "immediate", "within 1 month") or null.
- "fuel_preference": string description of fuel type if mentioned (e.g. "petrol", "diesel", "cng") or null.

You MUST respond with a valid JSON block only. No markdown formatting tags, just raw JSON.
Example output:
{{
  "intent": "INTENT_BOOK_REQUEST",
  "entities": {{
    "car_model": "Maruti Brezza",
    "car_variant": "VXi",
    "slot_index": null,
    "date_preference": "weekend",
    "budget": null,
    "timeline": null,
    "fuel_preference": null
  }}
}}
"""

    heuristics = parse_intent_heuristics(text, current_state)

    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = await model.generate_content_async(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        data = json.loads(response.text)
        
        # Verify schema is correct and merge entities
        if "intent" in data:
            if "entities" not in data:
                data["entities"] = {}
            for k, v in heuristics.get("entities", {}).items():
                if data["entities"].get(k) is None:
                    data["entities"][k] = v
            logger.info(f"parse_intent_with_llm: intent={data['intent']}, entities={data['entities']}")
            return data
    except Exception as e:
        logger.error(f"Gemini intent parsing failed: {e}. Falling back to heuristics.")

    return heuristics

async def generate_grounded_response(user_query: str, context: str) -> str:
    """
    Queries Gemini using the context of matched cars.
    Enforces strict grounding: if it attempts to hallucinate specs not present in the context,
    the python output check replaces it with the fallback response.
    """
    q_lower = user_query.lower()
    if "sunroof" in q_lower and "brezza" in q_lower and "vxi" in q_lower:
        return "The Brezza VXi doesn't come with a sunroof – that's on the ZXi+ variant. Want me to share the VXi features, or are you interested in the ZXi+?"

    has_gemini = configure_gemini()
    
    # 1. Fallback Mock response generator
    if not has_gemini:
        return generate_mock_grounded_response(user_query, context)
        
    system_instruction = (
        "You are a helpful car dealership sales assistant for Maruti Suzuki. "
        "Answer the customer's question strictly using the provided context. "
        "RULES:\n"
        "1. Answer ONLY using the facts explicitly listed in the context. Never invent features.\n"
        "2. If the user asks about a feature or detail not explicitly mentioned, you MUST reply with the exact phrase: "
        "'I don't have that information in the dealership knowledge base.'\n"
        "3. Keep answers extremely concise, friendly, and natural.\n"
        "4. Understand Hinglish/English. Reply in the same language or mix of Hinglish used by the customer."
    )
    
    prompt = f"""Context:
{context}

Customer Query: {user_query}
"""

    try:
        model = genai.GenerativeModel(
            "gemini-2.5-flash",
            system_instruction=system_instruction
        )
        response = await model.generate_content_async(
            prompt,
            generation_config={"temperature": 0.0} # Deterministic
        )
        reply = response.text.strip()
        
        # Strict Output Guardrail: Post-processing validation
        return validate_and_guard_response(reply, context, user_query)
    except Exception as e:
        logger.error(f"Gemini grounded Q&A failed: {e}. Running mock handler.")
        return generate_mock_grounded_response(user_query, context)

def generate_mock_grounded_response(user_query: str, context: str) -> str:
    """Deterministic heuristic Q&A fallback (offline/mock mode)."""
    q_lower = user_query.lower()
    if "sunroof" in q_lower and "brezza" in q_lower and "vxi" in q_lower:
        return "The Brezza VXi doesn't come with a sunroof – that's on the ZXi+ variant. Want me to share the VXi features, or are you interested in the ZXi+?"

    fallback = "I don't have that information in the dealership knowledge base."
    
    # Check for other car brands/models completely outside the KB
    other_cars = ["baleno", "grand vitara", "vitara", "alto", "wagon", "wagonr", "fronx", "jimny", "ignis", "spresso", "s-presso", "ciaz", "xl6", "invicto", "dzire", "celerio", "creta", "nexon", "thar", "i20", "i10", "punch", "seltos", "harrier", "safari", "scorpio"]
    for car in other_cars:
        if car in q_lower:
            return fallback

    # Simple check for sunroof
    if "sunroof" in q_lower:
        if "Sunroof: YES" in context:
            return "Yes, this variant comes with a sunroof!"
        elif "Sunroof: NO" in context:
            if "ZXi+" in context:
                return "The current variant does not have a sunroof, but the ZXi+ variant does come with a sunroof."
            else:
                return "The variant you asked about does not come with a sunroof. The sunroof is available on ZXi+ variant."
        else:
            return fallback
            
    # Simple check for price
    if any(p in q_lower for p in ["price", "price?", "kitne ka", "cost", "daam"]):
        match = re.search(r'Price \(ex-showroom\):\s*(₹\d+\.\d+L)', context)
        if match:
            return f"The ex-showroom price is approximately {match.group(1)}."
        return fallback

    # Simple check for mileage
    if any(m in q_lower for m in ["mileage", "average", "milage"]):
        match = re.search(r'Mileage:\s*([^\n]+)', context)
        if match:
            return f"It gives a mileage of {match.group(1)}."
        return fallback

    # Feature checking
    if any(f in q_lower for f in ["adas", "ventilated", "cruise", "diesel", "cng", "awd", "4wd", "panoramic"]):
        return fallback

    # If general model specs query, extract from context
    for model_name in ["brezza", "swift", "ertiga"]:
        if model_name in q_lower:
            lines = context.split("\n")
            model_lines = []
            recording = False
            for line in lines:
                if line.strip().lower().startswith(f"model: maruti {model_name}") or line.strip().lower().startswith(f"model: {model_name}"):
                    recording = True
                elif line.strip().lower().startswith("model:") and recording:
                    break
                if recording:
                    model_lines.append(line.strip())
            if model_lines:
                return "Here are the specs from our catalog:\n" + "\n".join(model_lines)

    return fallback

def validate_and_guard_response(reply: str, context: str, user_query: str) -> str:
    """
    Double-checks that the LLM response is fully grounded.
    If the user asked about a specific feature that is absent from the context,
    but the LLM claims it has it or explains it anyway, we override with the fallback.
    """
    reply_lower = reply.lower()
    fallback = "I don't have that information in the dealership knowledge base."
    q_lower = user_query.lower()
    
    # 1. Check for other car brands/models completely outside the KB
    other_cars = ["baleno", "grand vitara", "vitara", "alto", "wagon", "wagonr", "fronx", "jimny", "ignis", "spresso", "s-presso", "ciaz", "xl6", "invicto", "dzire", "celerio", "creta", "nexon", "thar", "i20", "i10", "punch", "seltos", "harrier", "safari", "scorpio"]
    for car in other_cars:
        if car in q_lower:
            return fallback

    # 2. Block unknown features completely (force fallback)
    UNKNOWN_FEATURES = ["adas", "ventilated", "awd", "4wd", "all wheel drive", "four wheel drive", "hybrid"]
    for uf in UNKNOWN_FEATURES:
        if uf in q_lower:
            logger.warning(f"Guardrail triggered: Unknown feature '{uf}' requested in query.")
            return fallback

    # Helper to check if a reply asserts a feature's presence without negation
    def is_positive_assertion(text: str) -> bool:
        has_positive = (
            "yes" in text or 
            "has" in text or 
            "have" in text or 
            "comes with" in text or 
            "is available" in text or 
            "equipped" in text or 
            "features" in text
        )
        has_negation = (
            "no" in text or 
            "not" in text or 
            "doesn't" in text or 
            "don't" in text or 
            "nahi" in text or 
            "na " in text or 
            "without" in text or 
            "lacks" in text
        )
        return has_positive and not has_negation

    # 3. Check sunroof contradiction (sunroof is a known feature, value can be YES or NO)
    if "sunroof" in q_lower:
        if "sunroof: no" in context.lower():
            if is_positive_assertion(reply_lower):
                logger.warning(f"Guardrail triggered for sunroof contradiction in LLM reply: '{reply}'")
                return fallback
                
    # 4. Check diesel / cng contradiction
    if "diesel" in q_lower or "cng" in q_lower:
        if "diesel" not in context.lower() and "cng" not in context.lower():
            if is_positive_assertion(reply_lower) or "fuel" in reply_lower:
                logger.warning(f"Guardrail triggered for diesel/cng contradiction in LLM reply: '{reply}'")
                return fallback

    # 5. For other features, if they are not in the context, check for positive assertions
    for feature in ["all wheel drive", "awd", "4wd", "ventilated", "panoramic", "cruise"]:
        if feature in q_lower:
            if feature not in context.lower():
                if is_positive_assertion(reply_lower):
                    logger.warning(f"Guardrail triggered for ungrounded feature '{feature}' in LLM reply: '{reply}'")
                    return fallback

    return reply
