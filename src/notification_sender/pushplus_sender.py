# -*- coding: utf-8 -*-
"""
PushPlus 发送提醒服务

职责：
1. 通过 PushPlus API 发送 PushPlus 消息

[定制改进 2026-10-09] 提升海外 runner（GitHub Actions）到国内 PushPlus 的送达成功率：
- 端点优先 HTTPS（回退 HTTP）
- 网络异常自动重试（默认 3 次，间隔 2 秒）
- 单次请求超时 10s -> 20s
- 错误日志包含端点与 HTTP 状态，便于诊断
"""
import logging
import time
from typing import Optional
from datetime import datetime
import requests

from src.config import Config
from src.formatters import chunk_content_by_max_bytes, strip_hidden_markdown_metadata


logger = logging.getLogger(__name__)

# 发送端点：优先 HTTPS（更稳定），HTTP 作为回退
_PUSHPLUS_API_URLS = (
    "https://www.pushplus.plus/send",
    "http://www.pushplus.plus/send",
)
_PUSHPLUS_MAX_ATTEMPTS = 3
_PUSHPLUS_RETRY_DELAY_SECONDS = 2
_PUSHPLUS_DEFAULT_TIMEOUT_SECONDS = 20


class PushplusSender:
    
    def __init__(self, config: Config):
        """
        初始化 PushPlus 配置

        Args:
            config: 配置对象
        """
        self._pushplus_token = getattr(config, 'pushplus_token', None)
        self._pushplus_topic = getattr(config, 'pushplus_topic', None)
        self._pushplus_max_bytes = getattr(config, 'pushplus_max_bytes', 20000)
        
    def send_to_pushplus(
        self,
        content: str,
        title: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        推送消息到 PushPlus

        PushPlus API 格式：
        POST https://www.pushplus.plus/send
        {
            "token": "用户令牌",
            "title": "消息标题",
            "content": "消息内容",
            "template": "html/txt/json/markdown"
        }

        PushPlus 特点：
        - 国内推送服务，免费额度充足
        - 支持微信公众号推送
        - 支持多种消息格式

        Args:
            content: 消息内容（Markdown 格式）
            title: 消息标题（可选）

        Returns:
            是否发送成功
        """
        if not self._pushplus_token:
            logger.warning("PushPlus Token 未配置，跳过推送")
            return False

        if title is None:
            date_str = datetime.now().strftime('%Y-%m-%d')
            title = f"📈 股票分析报告 - {date_str}"
        sanitized_content = strip_hidden_markdown_metadata(content).strip()

        try:
            content_bytes = len(sanitized_content.encode('utf-8'))
            if content_bytes > self._pushplus_max_bytes:
                logger.info(
                    "PushPlus 消息内容超长(%s字节/%s字符)，将分批发送",
                    content_bytes,
                    len(sanitized_content),
                )
                return self._send_pushplus_chunked(
                    sanitized_content,
                    title,
                    self._pushplus_max_bytes,
                    timeout_seconds=timeout_seconds,
                )

            return self._send_pushplus_message(
                sanitized_content,
                title,
                timeout_seconds=timeout_seconds,
            )
        except Exception as e:
            logger.error(f"发送 PushPlus 消息失败: {e}")
            return False

    def _send_pushplus_message(
        self,
        content: str,
        title: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        payload = {
            "token": self._pushplus_token,
            "title": title,
            "content": content,
            "template": "markdown",
        }

        if self._pushplus_topic:
            payload["topic"] = self._pushplus_topic

        timeout = timeout_seconds or _PUSHPLUS_DEFAULT_TIMEOUT_SECONDS
        last_error = "unknown"

        for api_url in _PUSHPLUS_API_URLS:
            for attempt in range(1, _PUSHPLUS_MAX_ATTEMPTS + 1):
                try:
                    response = requests.post(api_url, json=payload, timeout=timeout)
                except Exception as e:
                    last_error = f"网络异常({api_url}): {e}"
                    logger.warning(
                        "PushPlus 请求异常（%s，第 %s/%s 次）: %s",
                        api_url,
                        attempt,
                        _PUSHPLUS_MAX_ATTEMPTS,
                        e,
                    )
                    if attempt < _PUSHPLUS_MAX_ATTEMPTS:
                        time.sleep(_PUSHPLUS_RETRY_DELAY_SECONDS)
                    continue

                if response.status_code == 200:
                    try:
                        result = response.json()
                    except Exception as e:
                        last_error = f"响应解析失败({api_url}): {e}"
                        logger.error("PushPlus 响应解析失败（%s）: %s", api_url, e)
                        break

                    if result.get('code') == 200:
                        logger.info("PushPlus 消息发送成功（%s）", api_url)
                        return True

                    error_msg = result.get('msg', '未知错误')
                    logger.error(f"PushPlus 返回错误: {error_msg}（{api_url}）")
                    return False

                last_error = f"HTTP {response.status_code}({api_url})"
                logger.error(
                    "PushPlus 请求失败: HTTP %s（%s）",
                    response.status_code,
                    api_url,
                )
                break

        logger.error("PushPlus 发送最终失败: %s", last_error)
        return False

    def _send_pushplus_chunked(
        self,
        content: str,
        title: str,
        max_bytes: int,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """分批发送长 PushPlus 消息，给 JSON payload 预留空间。"""
        budget = max(1000, max_bytes - 1500)
        chunks = chunk_content_by_max_bytes(content, budget, add_page_marker=True)
        total_chunks = len(chunks)
        success_count = 0

        logger.info(f"PushPlus 分批发送：共 {total_chunks} 批")

        for i, chunk in enumerate(chunks):
            chunk_title = f"{title} ({i+1}/{total_chunks})" if total_chunks > 1 else title
            if self._send_pushplus_message(
                chunk, chunk_title, timeout_seconds=timeout_seconds
            ):
                success_count += 1
                logger.info(f"PushPlus 第 {i+1}/{total_chunks} 批发送成功")
            else:
                logger.error(f"PushPlus 第 {i+1}/{total_chunks} 批发送失败")

            if i < total_chunks - 1:
                time.sleep(1)

        return success_count == total_chunks
