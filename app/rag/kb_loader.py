import os
import json
import logging
from app.utils.normalization import normalize_variant

logger = logging.getLogger(__name__)

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
CARS_JSON_PATH = os.path.join(DATA_DIR, "cars.json")

class KnowledgeBase:
    _data = None

    @classmethod
    def load(cls):
        if cls._data is None:
            if not os.path.exists(CARS_JSON_PATH):
                logger.error(f"cars.json not found at {CARS_JSON_PATH}")
                # Fallback empty structure
                cls._data = {"oem": "Maruti Suzuki", "models": []}
            else:
                try:
                    with open(CARS_JSON_PATH, "r", encoding="utf-8") as f:
                        cls._data = json.load(f)
                    logger.info("Successfully loaded cars.json knowledge base.")
                except Exception as e:
                    logger.exception("Failed to load cars.json")
                    cls._data = {"oem": "Maruti Suzuki", "models": []}
        return cls._data

    @classmethod
    def get_models(cls):
        data = cls.load()
        return [model["name"] for model in data.get("models", [])]

    @classmethod
    def get_model_details(cls, model_name: str):
        data = cls.load()
        for model in data.get("models", []):
            if model["name"].lower() == model_name.lower():
                return model
        # Try soft match
        for model in data.get("models", []):
            if model_name.lower() in model["name"].lower() or model["name"].lower() in model_name.lower():
                return model
        return None

    @classmethod
    def get_variant_details(cls, model_name: str, variant_name: str):
        model = cls.get_model_details(model_name)
        if not model:
            return None
        
        norm_search = normalize_variant(variant_name)
        
        for var in model.get("variants", []):
            if normalize_variant(var["name"]) == norm_search:
                return var
        # Try soft match
        for var in model.get("variants", []):
            norm_var = normalize_variant(var["name"])
            if norm_search in norm_var or norm_var in norm_search:
                return var
        return None
