"""
security/ — Secret management and security utilities for Tinker.

Provides a unified interface to retrieve secrets from multiple backends:
  - Environment variables (default, always available)
  - HashiCorp Vault (for enterprise deployments)
  - AWS Secrets Manager (for AWS deployments)
  - Azure Key Vault (for Azure deployments)
  - Encrypted .env files (using python-dotenv[crypt])

Usage:
    from security.secrets import get_secret

    redis_password = get_secret("TINKER_REDIS_PASSWORD")
    ollama_api_key = get_secret("TINKER_OLLAMA_KEY")
"""
