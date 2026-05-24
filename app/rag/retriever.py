import re
from app.rag.kb_loader import KnowledgeBase

def retrieve_context(user_message: str) -> str:
    """
    Analyzes the user's message, extracts model and variant keywords,
    and returns a structured string of specifications.
    
    If no specific model/variant is matched, returns the specifications for all models
    so the LLM has broad context for Q&A.
    """
    kb = KnowledgeBase.load()
    user_message_lower = user_message.lower()
    
    # Identify which models are mentioned
    matched_models = []
    for model in kb.get("models", []):
        name_parts = model["name"].lower().split()
        # check if any key word (like brezza, swift, ertiga) matches
        for part in name_parts:
            if len(part) > 3 and part in user_message_lower:
                matched_models.append(model)
                break
            
    # Identify which variants are mentioned
    variants_in_kb = {"vxi", "zxi+", "zxi plus", "lxi", "zxi"}
    matched_variants = []
    for var in variants_in_kb:
        # Match word boundaries or simple substring
        if var in user_message_lower:
            # normalize "zxi plus" or "zxi+"
            norm_var = "zxi+" if "zxi" in var and ("+" in var or "plus" in var) else var
            matched_variants.append(norm_var)
            
    context_lines = []
    
    # If specific models were matched
    if matched_models:
        for model in matched_models:
            context_lines.append(f"Model: {model['name']}")
            # If specific variants were matched, return only those variants for the model
            # Otherwise return all variants for the model (allows comparison)
            for var in model.get("variants", []):
                var_name_lower = var["name"].lower()
                is_matched_var = False
                for mv in matched_variants:
                    if mv in var_name_lower or var_name_lower in mv:
                        is_matched_var = True
                        
                # If variant is matched, or no specific variant was mentioned, include it
                if is_matched_var or not matched_variants:
                    context_lines.append(f"  Variant: {var['name']}")
                    context_lines.append(f"    Engine: {var.get('engine', 'N/A')}")
                    context_lines.append(f"    Mileage: {var.get('mileage', 'N/A')}")
                    context_lines.append(f"    Transmission: {var.get('transmission', 'N/A')}")
                    context_lines.append(f"    Features: {', '.join(var.get('features', []))}")
                    context_lines.append(f"    Sunroof: {'YES' if var.get('sunroof', False) else 'NO'}")
                    context_lines.append(f"    Price (ex-showroom): {var.get('price', 'N/A')}")
                    context_lines.append(f"    Colors: {', '.join(var.get('colors', []))}")
    else:
        # Return everything as fallback since the KB is small (3 models)
        for model in kb.get("models", []):
            context_lines.append(f"Model: {model['name']}")
            for var in model.get("variants", []):
                context_lines.append(f"  Variant: {var['name']}")
                context_lines.append(f"    Engine: {var.get('engine', 'N/A')}")
                context_lines.append(f"    Mileage: {var.get('mileage', 'N/A')}")
                context_lines.append(f"    Transmission: {var.get('transmission', 'N/A')}")
                context_lines.append(f"    Features: {', '.join(var.get('features', []))}")
                context_lines.append(f"    Sunroof: {'YES' if var.get('sunroof', False) else 'NO'}")
                context_lines.append(f"    Price (ex-showroom): {var.get('price', 'N/A')}")
                context_lines.append(f"    Colors: {', '.join(var.get('colors', []))}")
                
    return "\n".join(context_lines)
