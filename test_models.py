import os
from groq import Groq

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Test openai/gpt-oss-20b
try:
    r = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[{"role": "user", "content": "Say hello in JSON"}],
        max_tokens=50,
        response_format={"type": "json_object"}
    )
    print("gpt-oss-20b works:", r.choices[0].message.content)
except Exception as e:
    print("gpt-oss-20b error:", e)

# Test qwen3.8-27b
try:
    r2 = client.chat.completions.create(
        model="qwen/qwen3.8-27b",
        messages=[{"role": "user", "content": "Say hello in JSON"}],
        max_tokens=50,
        response_format={"type": "json_object"}
    )
    print("qwen3.8-27b works:", r2.choices[0].message.content)
except Exception as e:
    print("qwen3.8-27b error:", e)

# Test groq/compound
try:
    r3 = client.chat.completions.create(
        model="groq/compound",
        messages=[{"role": "user", "content": "Say hello in JSON"}],
        max_tokens=50,
        response_format={"type": "json_object"}
    )
    print("groq/compound works:", r3.choices[0].message.content)
except Exception as e:
    print("groq/compound error:", e)
