"""Unit tests for LLMFactory."""

import pytest
from unittest.mock import patch, MagicMock

from smartinspector.llm.factory import LLMFactory


class TestLLMFactory:
    def setup_method(self):
        """Reset factory before each test."""
        LLMFactory.reset()

    def test_get_returns_same_instance_for_same_role(self):
        with patch("smartinspector.llm.factory.ChatOpenAI") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            llm1 = LLMFactory.get("test_role", temperature=0.1)
            llm2 = LLMFactory.get("test_role", temperature=0.1)

            assert llm1 is llm2
            assert mock_cls.call_count == 1

    def test_get_creates_different_instances_for_different_roles(self):
        with patch("smartinspector.llm.factory.ChatOpenAI") as mock_cls:
            mock1 = MagicMock()
            mock2 = MagicMock()
            mock_cls.side_effect = [mock1, mock2]

            llm1 = LLMFactory.get("role_a", temperature=0.1)
            llm2 = LLMFactory.get("role_b", temperature=0.1)

            assert llm1 is not llm2
            assert mock_cls.call_count == 2

    def test_get_creates_different_instances_for_different_overrides(self):
        with patch("smartinspector.llm.factory.ChatOpenAI") as mock_cls:
            mock1 = MagicMock()
            mock2 = MagicMock()
            mock_cls.side_effect = [mock1, mock2]

            llm1 = LLMFactory.get("test", temperature=0.1)
            llm2 = LLMFactory.get("test", temperature=0.5)

            assert llm1 is not llm2

    def test_get_with_tools(self):
        with patch("smartinspector.llm.factory.ChatOpenAI") as mock_cls:
            mock_llm = MagicMock()
            mock_bound = MagicMock()
            mock_llm.bind_tools.return_value = mock_bound
            mock_cls.return_value = mock_llm

            tools = [MagicMock(), MagicMock()]
            result = LLMFactory.get_with_tools("tool_role", tools, temperature=0)

            assert result is mock_bound
            mock_llm.bind_tools.assert_called_once_with(tools)

    def test_get_with_tools_reuses_base_llm(self):
        with patch("smartinspector.llm.factory.ChatOpenAI") as mock_cls:
            mock_llm = MagicMock()
            mock_cls.return_value = mock_llm

            LLMFactory.get("reuse_test", temperature=0.1)
            LLMFactory.get("reuse_test", temperature=0.1)

            assert mock_cls.call_count == 1

    def test_reset_clears_cache(self):
        with patch("smartinspector.llm.factory.ChatOpenAI") as mock_cls:
            mock1 = MagicMock()
            mock2 = MagicMock()
            mock_cls.side_effect = [mock1, mock2]

            LLMFactory.get("clear_test", temperature=0.1)
            LLMFactory.reset()
            llm = LLMFactory.get("clear_test", temperature=0.1)

            # After reset, a new instance should be created
            assert mock_cls.call_count == 2

    def test_get_uses_config_get_llm_kwargs(self):
        with patch("smartinspector.llm.factory.get_llm_kwargs") as mock_kwargs, \
             patch("smartinspector.llm.factory.ChatOpenAI") as mock_cls:
            mock_kwargs.return_value = {"model": "test-model", "base_url": "http://test"}
            mock_cls.return_value = MagicMock()

            LLMFactory.get("test", temperature=0.5)

            mock_kwargs.assert_called_once_with(role="test")
            mock_cls.assert_called_once_with(
                model="test-model",
                base_url="http://test",
                temperature=0.5,
            )

    def test_thread_safety(self):
        """Test that concurrent access doesn't create duplicate instances."""
        import threading

        with patch("smartinspector.llm.factory.ChatOpenAI") as mock_cls:
            mock_cls.return_value = MagicMock()

            results = []
            errors = []

            def get_llm():
                try:
                    llm = LLMFactory.get("concurrent_test", temperature=0.1)
                    results.append(id(llm))
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=get_llm) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(errors) == 0
            assert len(results) == 10
            # All threads should get the same instance
            assert len(set(results)) == 1
