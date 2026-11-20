import json
import unittest
from uuid import uuid4

import httpx

from hr_api.questions import QuestionContext, RequiredCriterion, YandexQuestionProvider
from question_generator import YandexGPTClient, YandexGPTSettings


class HRQuestionTests(unittest.TestCase):
    def context(self):
        return QuestionContext(response_id=uuid4(), vacancy_title="Developer",
            skills=[{"name": "Python", "level": "basic", "original": "Python"}],
            required_criteria=[RequiredCriterion(id=uuid4(), name="Python", description=None)], matching=None, missing_data=[])

    def question(self, criterion_id):
        return {"questions": [{"criterion_id": str(criterion_id), "question": "Какую задачу вы решали с Python?",
                               "expected_answer": "Пример задачи и личный вклад", "follow_up_hint": None}]}

    def execute(self, context, replies):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"result": {"alternatives": [{"status": "ALTERNATIVE_STATUS_FINAL",
                "message": {"text": json.dumps(replies.pop(0), ensure_ascii=False)}}]}})

        settings = YandexGPTSettings.model_validate({"folder_id": "test-folder", "api_key": "test-secret"})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            provider = YandexQuestionProvider(YandexGPTClient(settings, http_client=http))
            result = provider.generate(context)
        return result, requests

    def test_generation_review_contract_and_expected_answer(self):
        context = self.context()
        result, requests = self.execute(context, [self.question(context.required_criteria[0].id), {"approved": True, "issues": []}])
        self.assertEqual(len(requests), 2)
        self.assertEqual(result[0].position, 0)
        self.assertTrue(result[0].expected_answer)
        data = json.loads(requests[0]["messages"][1]["text"])
        self.assertEqual(data["skills"], context.skills)
        self.assertNotIn("source_json", data)
        self.assertEqual(requests[0]["completionOptions"]["temperature"], 0)

    def test_foreign_criterion_is_rejected_before_review(self):
        context = self.context()
        result, requests = self.execute(context, [self.question(uuid4()), self.question(context.required_criteria[0].id),
                                                  {"approved": True, "issues": []}])
        self.assertEqual(result[0].criterion_id, context.required_criteria[0].id)
        self.assertEqual(len(requests), 3)
        self.assertTrue(json.loads(requests[1]["messages"][1]["text"])["validation_feedback"])

    def test_review_rejection_is_repaired(self):
        context = self.context()
        draft = self.question(context.required_criteria[0].id)
        _, requests = self.execute(context, [draft, {"approved": False, "issues": ["Уточни практический аспект"]},
                                            draft, {"approved": True, "issues": []}])
        self.assertEqual(len(requests), 4)

    def test_no_required_criteria_does_not_call_model(self):
        context = self.context()
        context.required_criteria = []
        result, requests = self.execute(context, [])
        self.assertEqual(result, [])
        self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main()
