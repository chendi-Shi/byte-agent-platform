import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from byte_agent.model import Ollama, Scripted, prepare_template_variant
from byte_agent.runtime import Runtime
from byte_agent.skills import load_skill


class OptionsTests(unittest.TestCase):
    def test_cancel_before_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            trace = Runtime(folder, Scripted([]), {}).run("task", is_cancelled=lambda: True)
            self.assertEqual(trace["status"], "cancelled")
            self.assertEqual(trace["steps"], 0)
            self.assertEqual(trace["unknown_usage"], 0)

    def test_selected_skill_and_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "SKILL.md"
            path.write_text("---\nname: triage\ndescription: Test\n---\nObserve only.", encoding="utf-8")
            skill = load_skill(path)
            self.assertIn("Observe only.", skill.prompt("System"))
            self.assertEqual(len(skill.sha256), 64)
            path.write_text("x" * 16001)
            with self.assertRaises(ValueError):
                load_skill(path)

    def test_template_variant_preserves_source_and_repairs_json(self):
        with patch.object(Ollama, "request", side_effect=[{"template": "{{ .Function }} {{ .Function.Arguments }}"}, {"status": "success"}]) as request, patch.object(Ollama, "describe", return_value={"digest": "variant"}):
            result = prepare_template_variant("original", "variant")
            body = request.call_args_list[1].args[1]
            self.assertEqual(body["from"], "original")
            self.assertIn("{{ json .Function }}", body["template"])
            self.assertIn("{{ json .Function.Arguments }}", body["template"])
            self.assertFalse(result["weights_trained"])

    def test_invalid_provider_configuration(self):
        for options in ({"context": 100}, {"output_tokens": 5000}, {"timeout": 0}):
            with self.assertRaises(ValueError):
                Ollama("model", **options)

    def test_generation_configuration_changes_run_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Scripted([{"answer": "done"}])
            model.config = {"seed": 42}
            Runtime(folder, model, {}).run("task")
            model.config = {"seed": 43}
            with self.assertRaises(ValueError):
                Runtime(folder, model, {}).run("task")


if __name__ == "__main__":
    unittest.main()
