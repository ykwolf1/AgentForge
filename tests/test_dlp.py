"""DLP 敏感信息检测测试"""
import pytest
from agentforge.tools.dlp import DLPDetector


class TestDLPDetector:
    def setup_method(self):
        self.detector = DLPDetector()

    def test_api_key_masked(self):
        text = "my key is sk-abc123def456ghi789jkl012mno345"
        masked, findings = self.detector.scan(text)
        assert len(findings) == 1
        assert findings[0]["type"] == "API_KEY"
        assert "API_KEY_MASKED" in masked
        assert "sk-abc123" not in masked

    def test_email_masked(self):
        text = "contact me at user@example.com please"
        masked, findings = self.detector.scan(text)
        assert any(f["type"] == "EMAIL" for f in findings)
        assert "EMAIL_MASKED" in masked

    def test_phone_cn_masked(self):
        text = "call 13812345678 or 15998765432"
        masked, findings = self.detector.scan(text)
        assert len(findings) == 2
        assert "PHONE_CN_MASKED" in masked

    def test_no_sensitive_info(self):
        text = "this is a normal message about coding"
        masked, findings = self.detector.scan(text)
        assert findings == []
        assert masked == text

    def test_multiple_types(self):
        text = "key: sk-abc123def456ghi789jkl012mno345 email: test@test.com phone: 13800001111"
        masked, findings = self.detector.scan(text)
        types = [f["type"] for f in findings]
        assert "API_KEY" in types
        assert "EMAIL" in types
        assert "PHONE_CN" in types

    def test_empty_text(self):
        masked, findings = self.detector.scan("")
        assert findings == []
        assert masked == ""

    def test_aws_key(self):
        text = "aws key AKIAIOSFODNN7EXAMPLE here"
        masked, findings = self.detector.scan(text)
        assert any(f["type"] == "AWS_KEY" for f in findings)

    def test_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
        masked, findings = self.detector.scan(text)
        assert any(f["type"] == "PRIVATE_KEY" for f in findings)
