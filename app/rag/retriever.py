"""
retrieve_context(user_message) → structured context string for RAG grounding.

Design decisions:
- Model matched via alias dict (avoids >3-char split ambiguity with "maruti" collisions).
- Variant matched using normalize_variant so "zxi plus", "ZXi+", "zxiplus" all resolve.
- Context keys are written WITHOUT leading spaces so heuristic line-starts in
  generate_grounded_response() work correctly (startswith checks).
- Falls back to ALL models when no model alias is found (small KB, wide context is fine).
"""
from app.rag.kb_loader import KnowledgeBase
from app.utils.normalization import normalize_variant

# Maps common aliases to canonical model names as stored in cars.json
MODEL_ALIASES: dict[str, str] = {
    "brezza": "Maruti Brezza",
    "swift":  "Maruti Swift",
    "ertiga": "Maruti Ertiga",
}


def _build_variant_block(var: dict) -> list[str]:
    """Returns context lines for a single variant dict."""
    sunroof_val = "YES" if var.get("sunroof") else "NO"
    features_str = ", ".join(var.get("features", []))
    colors_str   = ", ".join(var.get("colors", []))
    return [
        f"Variant: {var['name']}",
        f"Engine: {var.get('engine', 'N/A')}",
        f"Mileage: {var.get('mileage', 'N/A')}",
        f"Transmission: {var.get('transmission', 'N/A')}",
        f"Features: {features_str}",
        f"Sunroof: {sunroof_val}",
        f"Price (ex-showroom): {var.get('price', 'N/A')}",
        f"Colors: {colors_str}",
        "",  # blank separator between variants
    ]


def retrieve_context(user_message: str) -> str:
    """
    Returns a structured context string scoped to the car model (and optionally
    variant) mentioned in the user message.  If nothing matches, returns full KB.
    """
    kb = KnowledgeBase.load()
    text = user_message.lower()
    norm_text = normalize_variant(text)

    # ------------------------------------------------------------------
    # 1. Identify the model being discussed
    # ------------------------------------------------------------------
    matched_model_name: str | None = None
    for alias, canonical in MODEL_ALIASES.items():
        if alias in text:
            matched_model_name = canonical
            break

    # ------------------------------------------------------------------
    # 2. Identify the variant being discussed (only if model is known)
    # ------------------------------------------------------------------
    matched_variant_name: str | None = None
    if matched_model_name:
        model_obj = KnowledgeBase.get_model_details(matched_model_name)
        if model_obj:
            for var in model_obj.get("variants", []):
                norm_var = normalize_variant(var["name"])
                # Accept: exact normalized match, or one is contained in the other
                if norm_var == norm_text or norm_var in norm_text or norm_text in norm_var:
                    matched_variant_name = var["name"]
                    break

    # ------------------------------------------------------------------
    # 3. Build context
    # ------------------------------------------------------------------
    context_lines: list[str] = []

    for model in kb.get("models", []):
        # Skip models not mentioned (unless no model was matched → full KB)
        if matched_model_name and model["name"] != matched_model_name:
            continue

        context_lines.append(f"Model: {model['name']}")

        for var in model.get("variants", []):
            # Skip variants not mentioned (unless no variant was matched → all variants)
            if matched_variant_name and var["name"] != matched_variant_name:
                continue
            context_lines.extend(_build_variant_block(var))

    return "\n".join(context_lines)
