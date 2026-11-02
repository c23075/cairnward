"""Templates for hosted model providers (FR-BACK-5, D70): `cairnward backend template <name>`
prints a block for backends.toml. Nothing is enabled by printing it.

Every template is `location = "hosted"` (denied unless `hosted_backends` allows it, never
used for locked conversations) and takes its key from an environment variable (keys are
never written into config files). `trains_on_prompts` is set conservatively: true for
free tiers and wherever Cairnward can't vouch that the provider's API terms rule out
training on your prompts. Check the provider's current terms; set it to false only if
they do. With `training_backends = "scratch_only"` (the Balanced and Strict default) such
backends answer only in environments marked `scratch = true`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    title: str
    url: str
    key_env: str
    trains_on_prompts: bool
    note: str


PROVIDERS: dict[str, Provider] = {p.name: p for p in [
    Provider("openrouter", "OpenRouter (paid models)", "https://openrouter.ai/api/v1",
             "OPENROUTER_API_KEY", False,
             "Routes to many providers: in OpenRouter's privacy settings, turn off providers "
             "that may train on or log your prompts; otherwise set trains_on_prompts = true."),
    Provider("openrouter-free", "OpenRouter (:free models)", "https://openrouter.ai/api/v1",
             "OPENROUTER_API_KEY", True,
             "Free models are rate-limited and their providers may log or train on prompts."),
    Provider("deepseek", "DeepSeek API", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY",
             True, "Pay-as-you-go; check DeepSeek's current terms on the use of API inputs."),
    Provider("alibaba", "Alibaba Model Studio (international)",
             "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", True,
             "Qwen models; new accounts get a one-time free quota. Check the current terms."),
    Provider("groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", True,
             "Free tier is rate-limited. Check Groq's current terms on the use of API data."),
    Provider("cerebras", "Cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", True,
             "Free tier is rate-limited. Check the current terms on the use of API data."),
    Provider("gemini-free", "Google Gemini API (free tier)",
             "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", True,
             "Google uses free-tier prompts to improve its products."),
    Provider("gemini", "Google Gemini API (paid tier)",
             "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", False,
             "Paid tier (billing enabled); check Google's current Gemini API terms."),
]}  # fmt: skip


def template(name: str) -> str:
    p = PROVIDERS.get(name)
    if p is None:
        raise KeyError(f"unknown provider '{name}': {', '.join(PROVIDERS)}")
    host = p.url.split("//", 1)[1].split("/", 1)[0]
    return f"""# {p.title} -- add to backends.toml, then set {p.key_env} in your environment.
# {p.note}
[backends.{p.name}]
driver = "openai_compat"
url = "{p.url}"
location = "hosted"
api_key = "env:{p.key_env}"
trains_on_prompts = {str(p.trains_on_prompts).lower()}
max_parallel = 2
timeout_s = 300

# Use it in an environment (environments/<name>.toml), with a model id from the provider:
#   backend = "{p.name}"
#   models = {{ main = "<model id>", fast = "<model id>" }}
#   [privacy]
#   hosted_backends = "ask"          # or "allow"; the default presets deny or ask
#   network_allow_hosts = ["{host}"]  # needed where network = "allowlist"
""" + ("#   scratch = true                   # this backend may train on prompts (see above)\n"
       if p.trains_on_prompts else "")  # fmt: skip
