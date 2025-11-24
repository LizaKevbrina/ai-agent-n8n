"""
Secrets Helper - Утилита для безопасного чтения secrets
Используется во всех микросервисах
"""

import os
import logging

logger = logging.getLogger(__name__)

def load_secret(env_var_name: str, fallback_env_var: str = None) -> str:
    """
    Загружает секрет из файла или environment variable
    
    Приоритет:
    1. Файл (Docker secrets): /run/secrets/secret_name
    2. Environment variable с суффиксом _FILE
    3. Fallback environment variable (для локальной разработки)
    
    Args:
        env_var_name: Имя переменной окружения с путём к файлу (например, "YANDEX_API_KEY_FILE")
        fallback_env_var: Имя переменной для локальной разработки (например, "YANDEX_API_KEY")
    
    Returns:
        str: Значение секрета
    
    Raises:
        ValueError: Если секрет не найден
    """
    # 1. Попытка прочитать из файла (Docker secrets)
    secret_file = os.getenv(env_var_name)
    if secret_file and os.path.exists(secret_file):
        try:
            with open(secret_file, 'r') as f:
                secret = f.read().strip()
                if secret:
                    logger.info(f"Loaded secret from file: {secret_file}")
                    return secret
        except Exception as e:
            logger.error(f"Failed to read secret from {secret_file}: {e}")
    
    # 2. Fallback на environment variable (для локальной разработки)
    if fallback_env_var:
        secret = os.getenv(fallback_env_var)
        if secret:
            logger.warning(
                f"Using fallback env var {fallback_env_var}. "
                f"For production, use Docker secrets!"
            )
            return secret
    
    # 3. Секрет не найден
    raise ValueError(
        f"Secret not found! "
        f"Set {env_var_name} to point to a secrets file, "
        f"or set {fallback_env_var} (not recommended for production)"
    )


def load_all_secrets() -> dict:
    """
    Загружает все секреты для микросервиса
    
    Returns:
        dict: Словарь с секретами
    """
    secrets = {}
    
    # YandexGPT credentials
    try:
        secrets['YANDEX_API_KEY'] = load_secret('YANDEX_API_KEY_FILE', 'YANDEX_API_KEY')
    except ValueError:
        logger.warning("YANDEX_API_KEY not configured")
    
    try:
        secrets['YANDEX_FOLDER_ID'] = load_secret('YANDEX_FOLDER_ID_FILE', 'YANDEX_FOLDER_ID')
    except ValueError:
        logger.warning("YANDEX_FOLDER_ID not configured")
    
    # Supabase credentials
    try:
        secrets['SUPABASE_URL'] = load_secret('SUPABASE_URL_FILE', 'SUPABASE_URL')
    except ValueError:
        logger.warning("SUPABASE_URL not configured")
    
    try:
        secrets['SUPABASE_KEY'] = load_secret('SUPABASE_KEY_FILE', 'SUPABASE_KEY')
    except ValueError:
        logger.warning("SUPABASE_KEY not configured")
    
    # Postgres password
    try:
        secrets['POSTGRES_PASSWORD'] = load_secret('POSTGRES_PASSWORD_FILE', 'POSTGRES_PASSWORD')
    except ValueError:
        logger.warning("POSTGRES_PASSWORD not configured")
    
    return secrets
