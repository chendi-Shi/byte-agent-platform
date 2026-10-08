"""Ollama transport: provider token counts are preserved, never estimated as zero."""
import json
import urllib.request


class Ollama:
    fixture = False

    def __init__(self, model, base_url="http://127.0.0.1:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def request(self, path, body):
        request = urllib.request.Request(self.base_url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)

    def describe(self):
        def get(path):
            with urllib.request.urlopen(self.base_url + path, timeout=10) as response:
                return json.load(response)
        models = get("/api/tags")["models"]
        name = self.model if ":" in self.model else self.model + ":latest"
        entry = next((m for m in models if m["name"] == name), None)
        if entry is None:
            raise ValueError("model is not installed")
        return {"name": entry["name"], "digest": entry["digest"], "details": entry.get("details"), "ollama": get("/api/version")}

    def embed(self, texts):
        return self.request("/api/embed", {"model": self.model, "input": texts})["embeddings"]

    def complete(self, messages, tools):
        response = self.request("/api/chat", {"model": self.model, "messages": messages, "stream": False,
                                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}} for t in tools],
                                "think": False,
                                "options": {"temperature": 0, "seed": 42, "num_predict": 1024, "num_ctx": 16384}})
        message = response["message"]
        calls = [{"name": c["function"]["name"], "arguments": c["function"]["arguments"]} for c in message.get("tool_calls", [])]
        usage = {"input": response.get("prompt_eval_count"), "output": response.get("eval_count")}
        return {"answer": message.get("content", ""), "calls": calls, "usage": usage, "message": message}


class Scripted:
    """Engineering fixture, explicitly excluded from model-performance claims."""
    fixture = True

    def __init__(self, decisions):
        self.decisions = decisions

    def complete(self, messages, tools):
        index = sum(m["role"] == "assistant" for m in messages)
        if index >= len(self.decisions):
            raise ValueError("fixture exhausted")
        result = dict(self.decisions[index])
        result.setdefault("calls", [])
        result.setdefault("answer", "")
        result["usage"] = {"input": None, "output": None}
        result["message"] = {"role": "assistant", "content": result["answer"]}
        if result["calls"]:
            result["message"]["tool_calls"] = [{"function": c} for c in result["calls"]]
        return result
