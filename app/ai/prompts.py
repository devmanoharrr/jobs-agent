PROMPT_VERSION = "v1"
TASK_TYPE = "enrichment"

SYSTEM_PROMPT = (
    "You extract structured job facts. Do not invent missing facts.\n"
    "Return valid JSON only. Unknown values must be null or []."
)


def user_prompt(title: str, location: str, description: str) -> str:
    return (
        "Extract:\n"
        "- normalized_title\n"
        "- role_category\n"
        "- seniority\n"
        "- skills (max 20, only explicitly mentioned or directly required)\n"
        "- experience_min\n"
        "- experience_max\n"
        "- work_mode\n"
        "- india_eligibility: india | remote_india | not_india | unknown\n"
        "\n"
        "JOB:\n"
        f"{title}\n"
        f"{location}\n"
        f"{description}\n"
    )
