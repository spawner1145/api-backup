import httpx
import json
import mimetypes
import asyncio
import base64
import os
import uuid
from typing import AsyncGenerator, Dict, List, Optional, Union, Callable
import aiofiles
import logging
import tempfile
from openai import AsyncOpenAI

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class OpenAIAPI:
    def __init__(
        self,
        apikey: str,
        baseurl: str = "https://api-inference.modelscope.cn",
        model: str = "deepseek-ai/DeepSeek-R1",
        proxies: Optional[Dict[str, str]] = None
    ):
        self.apikey = apikey
        self.baseurl = baseurl.rstrip('/')
        self.model = model
        self.client = AsyncOpenAI(
            api_key=apikey,
            base_url=baseurl,
            http_client=httpx.AsyncClient(proxies=proxies, timeout=60.0) if proxies else None
        )

    async def upload_file(self, file_path: str, display_name: Optional[str] = None) -> Dict[str, Union[str, None]]:
        """上传单个文件，使用 client.files.create，目的为 user_data"""
        try:
            file_size = os.path.getsize(file_path)
            if file_size > 32 * 1024 * 1024:  # 32MB 限制
                raise ValueError(f"文件 {file_path} 大小超过 32MB 限制")
        except FileNotFoundError:
            logger.error(f"文件 {file_path} 不存在")
            return {"fileId": None, "mimeType": None, "error": f"文件 {file_path} 不存在"}

        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "application/octet-stream"
            logger.warning(f"无法检测文件 {file_path} 的 MIME 类型，使用默认值: {mime_type}")

        supported_mime_types = [
            "application/pdf", "image/jpeg", "image/png", "image/webp", "image/gif"
        ]
        if mime_type not in supported_mime_types:
            logger.warning(f"MIME 类型 {mime_type} 可能不受支持，可能导致处理失败")

        try:
            async with aiofiles.open(file_path, 'rb') as f:
                file = await self.client.files.create(
                    file=(display_name or os.path.basename(file_path), await f.read(), mime_type),
                    purpose="user_data"
                )
                file_id = file.id
                logger.info(f"文件 {file_path} 上传成功，ID: {file_id}")
                return {"fileId": file_id, "mimeType": mime_type, "error": None}
        except Exception as e:
            logger.error(f"文件 {file_path} 上传失败: {str(e)}")
            return {"fileId": None, "mimeType": mime_type, "error": str(e)}

    async def upload_files(self, file_paths: List[str], display_names: Optional[List[str]] = None) -> List[Dict[str, Union[str, None]]]:
        """并行上传多个文件"""
        if not file_paths:
            raise ValueError("文件路径列表不能为空")

        if display_names and len(display_names) != len(file_paths):
            raise ValueError("display_names 长度必须与 file_paths 一致")

        tasks = [self.upload_file(file_paths[idx], display_names[idx] if display_names else None) for idx in range(len(file_paths))]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        final_results = []
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"上传文件 {file_paths[idx]} 失败: {str(result)}")
                final_results.append({"fileId": None, "mimeType": None, "error": str(result)})
            else:
                final_results.append(result)
        return final_results

    async def prepare_inline_image(self, file_path: str, detail: str = "auto") -> Dict[str, Union[Dict, None]]:
        """将单个图片转换为 Base64 编码的 input_image"""
        try:
            file_size = os.path.getsize(file_path)
            if file_size > 20 * 1024 * 1024:  # 20MB 限制
                raise ValueError(f"文件 {file_path} 过大，超过 20MB 限制")

            mime_type, _ = mimetypes.guess_type(file_path)
            if not mime_type or mime_type not in ["image/jpeg", "image/png", "image/webp", "image/gif"]:
                mime_type = "image/jpeg"
                logger.warning(f"无效图片 MIME 类型，使用默认值: {mime_type}")

            async with aiofiles.open(file_path, 'rb') as f:
                file_content = await f.read()
            base64_data = base64.b64encode(file_content).decode('utf-8')
            return {
                "input_image": {
                    "image_url": f"data:{mime_type};base64,{base64_data}",
                    "detail": detail
                }
            }
        except Exception as e:
            logger.error(f"处理图片 {file_path} 失败: {str(e)}")
            return {"input_image": None, "error": str(e)}

    async def prepare_inline_image_batch(self, file_paths: List[str], detail: str = "auto") -> List[Dict[str, Union[Dict, None]]]:
        """将多个图片转换为 Base64 编码的 input_image 列表"""
        if not file_paths:
            raise ValueError("文件路径列表不能为空")

        results = []
        for file_path in file_paths:
            result = await self.prepare_inline_image(file_path, detail)
            results.append(result)
        return results

    async def _execute_tool(
        self,
        tool_calls: List[Dict],
        tools: Dict[str, Callable]
    ) -> List[Dict]:
        """执行工具调用并返回响应，遵循 OpenAI 格式"""
        tool_responses = []
        for tool_call in tool_calls:
            name = tool_call.function.name
            if not name:
                logger.error(f"工具调用缺少名称: {tool_call}")
                continue
            tool_call_id = tool_call.id or f"call_{uuid.uuid4()}"
            args = json.loads(tool_call.function.arguments)
            logger.info(f"执行工具调用: {name}, 参数: {args}, ID: {tool_call_id}")
            func = tools.get(name)
            if func:
                try:
                    if asyncio.iscoroutinefunction(func):
                        result = await func(**args)
                    else:
                        result = func(**args)
                    logger.info(f"工具结果: {name} 返回 {result}, ID: {tool_call_id}")
                    tool_response = {
                        "role": "tool",
                        "content": json.dumps(result),
                        "tool_call_id": tool_call_id
                    }
                    tool_responses.append((tool_response, tool_call_id))
                except Exception as e:
                    result = f"函数 {name} 执行失败: {str(e)}"
                    logger.error(f"工具错误: {result}, ID: {tool_call_id}")
                    tool_response = {
                        "role": "tool",
                        "content": json.dumps({"error": result}),
                        "tool_call_id": tool_call_id
                    }
                    tool_responses.append((tool_response, tool_call_id))
            else:
                logger.error(f"未找到工具: {name}, ID: {tool_call_id}")
                tool_response = {
                    "role": "tool",
                    "content": json.dumps({"error": f"未找到工具 {name}"}),
                    "tool_call_id": tool_call_id
                }
                tool_responses.append((tool_response, tool_call_id))
        return tool_responses

    async def _chat_api(
        self,
        messages: List[Dict],
        stream: bool,
        tools: Optional[Dict[str, Callable]] = None,
        max_output_tokens: Optional[int] = None,
        system_instruction: Optional[str] = None,
        topp: Optional[float] = None,
        temperature: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        stop_sequences: Optional[List[str]] = None,
        response_format: Optional[Dict] = None,
        seed: Optional[int] = None,
        response_logprobs: Optional[bool] = None,
        logprobs: Optional[int] = None,
        retries: int = 3
    ) -> AsyncGenerator[str, None]:
        """核心 API 调用逻辑，遵循 OpenAI 标准，支持 reasoning_content 但不记录到历史"""
        original_model = self.model

        # 验证参数
        if topp is not None and (topp < 0 or topp > 1):
            raise ValueError("top_p 必须在 0 到 1 之间")
        if temperature is not None and (temperature < 0 or temperature > 2):
            raise ValueError("temperature 必须在 0 到 2 之间")
        if presence_penalty is not None and (presence_penalty < -2 or presence_penalty > 2):
            raise ValueError("presence_penalty 必须在 -2 到 2 之间")
        if frequency_penalty is not None and (frequency_penalty < -2 or frequency_penalty > 2):
            raise ValueError("frequency_penalty 必须在 -2 到 2 之间")
        if logprobs is not None and (logprobs < 0 or logprobs > 20):
            raise ValueError("logprobs 必须在 0 到 20 之间")

        # 构造消息
        api_messages = []
        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")
            if isinstance(content, str):
                api_content = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                api_content = []
                for part in content:
                    if "text" in part:
                        api_content.append({"type": "text", "text": part["text"]})
                    elif "input_file" in part:
                        api_content.append({
                            "type": "input_file",
                            "file_id": part["input_file"]["file_id"]
                        } if "file_id" in part["input_file"] else {
                            "type": "input_file",
                            "filename": part["input_file"]["filename"],
                            "file_data": part["input_file"]["file_data"]
                        })
                    elif "input_image" in part:
                        api_content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": part["input_image"]["image_url"],
                                "detail": part["input_image"].get("detail", "auto")
                            }
                        })
            else:
                raise ValueError(f"无效的消息内容格式: {content}")
            api_msg = {
                "role": role,
                "content": api_content
            }
            if "tool_calls" in msg:
                api_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"]
                        }
                    } for tc in msg["tool_calls"]
                ]
            if "tool_call_id" in msg:
                api_msg["tool_call_id"] = msg["tool_call_id"]
            logger.debug(f"构造消息: {json.dumps(api_msg, ensure_ascii=False)}")
            api_messages.append(api_msg)

        # 构造请求参数
        request_params = {
            "model": self.model,
            "messages": api_messages,
            "stream": stream
        }
        if max_output_tokens is not None:
            request_params["max_tokens"] = max_output_tokens
        if topp is not None:
            request_params["top_p"] = topp
        if temperature is not None:
            request_params["temperature"] = temperature
        if stop_sequences is not None:
            request_params["stop"] = stop_sequences
        if presence_penalty is not None:
            request_params["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            request_params["frequency_penalty"] = frequency_penalty
        if seed is not None:
            request_params["seed"] = seed
        if response_logprobs is not None:
            request_params["logprobs"] = response_logprobs
            if logprobs is not None:
                request_params["top_logprobs"] = logprobs
        if response_format:
            request_params["response_format"] = response_format

        if tools is not None:
            tool_definitions = []
            for name, func in tools.items():
                params = {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
                if hasattr(func, "__code__"):
                    param_names = func.__code__.co_varnames[:func.__code__.co_argcount]
                    for param in param_names:
                        params["properties"][param] = {"type": "string"}
                        params["required"].append(param)
                else:
                    params["properties"] = {"arg": {"type": "string"}}
                    params["required"] = ["arg"]
                tool_definitions.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": getattr(func, "__doc__", f"调用 {name} 函数"),
                        "parameters": params
                    }
                })
            request_params["tools"] = tool_definitions

        #logger.info(f"发送 POST 请求体: {json.dumps(request_params, ensure_ascii=False, indent=2)}")

        # 执行请求
        if stream:
            assistant_content = ""
            tool_calls_buffer = []
            async for chunk in await self.client.chat.completions.create(**request_params):
                # logger.debug(f"流式响应分片: {json.dumps(chunk.dict(), ensure_ascii=False)}")  # 注释掉原始返回内容日志
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                        yield f"REASONING: {delta.reasoning_content}"
                    if delta.content:
                        yield delta.content
                        assistant_content += delta.content
                    elif delta.tool_calls:
                        for tool_call in delta.tool_calls:
                            if tool_call and tool_call.function:
                                tool_call_id = tool_call.id or f"call_{uuid.uuid4()}"
                                try:
                                    arguments = tool_call.function.arguments or "{}"
                                    json.loads(arguments)
                                    tool_calls_buffer.append({
                                        "id": tool_call_id,
                                        "type": "function",
                                        "function": {
                                            "name": tool_call.function.name,
                                            "arguments": arguments
                                        }
                                    })
                                    logger.info(f"工具调用: {tool_call.function.name}, 参数: {arguments}, ID: {tool_call_id}")
                                except json.JSONDecodeError:
                                    logger.error(f"工具调用 {tool_call.function.name} 的 arguments 无效: {arguments}")
                                    continue
                        if chunk.choices[0].finish_reason == "tool_calls" and tool_calls_buffer:
                            assistant_message = {
                                "role": "assistant",
                                "content": "Tool calls executed",
                                "tool_calls": tool_calls_buffer
                            }
                            api_messages.append(assistant_message)
                            messages.append(assistant_message)
                            tool_responses = await self._execute_tool(
                                [
                                    type('ToolCall', (), {
                                        'id': tc["id"],
                                        'function': type('Function', (), {
                                            'name': tc["function"]["name"],
                                            'arguments': tc["function"]["arguments"]
                                        })()
                                    })() for tc in tool_calls_buffer
                                ],
                                tools
                            )
                            for tool_response, tool_call_id in tool_responses:
                                tool_message = {
                                    "role": "tool",
                                    "content": tool_response["content"],
                                    "tool_call_id": tool_call_id
                                }
                                api_messages.append(tool_message)
                                messages.append(tool_message)
                            second_request_params = request_params.copy()
                            second_request_params["messages"] = api_messages
                            second_request_params["stream"] = False
                            try:
                                response = await self.client.chat.completions.create(**second_request_params)
                                # logger.debug(f"第二次 API 调用响应: {json.dumps(response.dict(), ensure_ascii=False)}")  # 注释掉原始返回内容日志
                                choice = response.choices[0]
                                message = choice.message
                                assistant_message = {
                                    "role": "assistant",
                                    "content": message.content or ""
                                }
                                messages.append(assistant_message)
                                if hasattr(message, 'reasoning_content') and message.reasoning_content:
                                    yield f"REASONING: {message.reasoning_content}"
                                if message.content:
                                    yield message.content
                            except Exception as e:
                                logger.error(f"第二次 API 调用失败: {str(e)}")
                                yield f"错误: 无法获取最终响应 - {str(e)}"
                                messages.append({"role": "assistant", "content": f"错误: {str(e)}"})
                            tool_calls_buffer = []
                    if chunk.choices[0].finish_reason in ["stop", "length"]:
                        if assistant_content:
                            messages.append({"role": "assistant", "content": assistant_content})
                        assistant_content = ""
        else:
            for attempt in range(retries):
                try:
                    response = await self.client.chat.completions.create(**request_params)
                    # logger.debug(f"非流式响应体: {json.dumps(response.dict(), ensure_ascii=False)}")  # 注释掉原始返回内容日志
                    choice = response.choices[0]
                    message = choice.message
                    if message.tool_calls:
                        tool_calls = [
                            {
                                "id": tc.id or f"call_{uuid.uuid4()}",
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments
                                }
                            } for tc in message.tool_calls
                        ]
                        assistant_message = {
                            "role": "assistant",
                            "content": "Tool calls executed",
                            "tool_calls": tool_calls
                        }
                        api_messages.append(assistant_message)
                        messages.append(assistant_message)
                        tool_responses = await self._execute_tool(message.tool_calls, tools)
                        for tool_response, tool_call_id in tool_responses:
                            tool_message = {
                                "role": "tool",
                                "content": tool_response["content"],
                                "tool_call_id": tool_call_id
                            }
                            api_messages.append(tool_message)
                            messages.append(tool_message)
                        second_request_params = request_params.copy()
                        second_request_params["messages"] = api_messages
                        second_request_params["stream"] = False
                        response = await self.client.chat.completions.create(**second_request_params)
                        # logger.debug(f"第二次 API 调用响应: {json.dumps(response.dict(), ensure_ascii=False)}")  # 注释掉原始返回内容日志
                        choice = response.choices[0]
                        message = choice.message
                        assistant_message = {
                            "role": "assistant",
                            "content": message.content or ""
                        }
                        messages.append(assistant_message)
                        if hasattr(message, 'reasoning_content') and message.reasoning_content:
                            yield f"REASONING: {message.reasoning_content}"
                        if message.content:
                            yield message.content
                    else:
                        assistant_message = {
                            "role": "assistant",
                            "content": message.content or ""
                        }
                        if response_logprobs and choice.logprobs:
                            assistant_message["logprobs"] = choice.logprobs.content
                            messages.append(assistant_message)
                            if hasattr(message, 'reasoning_content') and message.reasoning_content:
                                yield f"REASONING: {message.reasoning_content}"
                            yield f"{message.content or ''}\nLogprobs: {json.dumps(choice.logprobs.content, ensure_ascii=False)}"
                        else:
                            messages.append(assistant_message)
                            if hasattr(message, 'reasoning_content') and message.reasoning_content:
                                yield f"REASONING: {message.reasoning_content}"
                            if message.content:
                                yield message.content
                    break
                except Exception as e:
                    logger.error(f"API 调用失败 (尝试 {attempt+1}/{retries}): {str(e)}")
                    if attempt == retries - 1:
                        raise
                    await asyncio.sleep(2 ** attempt)

        self.model = original_model

    async def chat(
        self,
        messages: Union[str, List[Dict[str, any]]],
        stream: bool = False,
        tools: Optional[Dict[str, Callable]] = None,
        max_output_tokens: Optional[int] = None,
        system_instruction: Optional[str] = None,
        topp: Optional[float] = None,
        temperature: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        stop_sequences: Optional[List[str]] = None,
        response_format: Optional[Dict] = None,
        seed: Optional[int] = None,
        response_logprobs: Optional[bool] = None,
        logprobs: Optional[int] = None,
        retries: int = 3
    ) -> AsyncGenerator[str, None]:
        """发起聊天请求，支持多文件和多图片输入"""
        if isinstance(messages, str):
            messages = [{"role": "user", "content": [{"type": "text", "text": messages}]}]
        if system_instruction:
            messages.insert(0, {"role": "system", "content": [{"type": "text", "text": system_instruction}]})

        async for part in self._chat_api(
            messages, stream, tools, max_output_tokens,
            system_instruction, topp, temperature,
            presence_penalty, frequency_penalty,
            stop_sequences, response_format,
            seed, response_logprobs, logprobs,
            retries
        ):
            yield part

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.client.aclose()

# 示例工具函数
async def schedule_meeting(start_time: str, duration: str, attendees: str) -> str:
    """安排一个会议，参数包括开始时间、持续时间和与会者"""
    return f"会议已安排：开始时间 {start_time}，持续时间 {duration}，与会者 {attendees}。"

async def get_weather(location: str) -> str:
    """获取指定地点的天气信息"""
    return f"{location} 的天气是晴天，温度 25°C。"

async def get_time(city: str) -> str:
    """获取指定城市的当前时间"""
    return f"{city} 的当前时间是 2025 年 4 月 24 日 13:00。"

async def send_email(to: str, body: str) -> str:
    """发送电子邮件"""
    return f"邮件已发送至 {to}，内容：{body}。"

# 主函数
async def main():
    api = OpenAIAPI(
        apikey="",
        baseurl="https://api-inference.modelscope.cn/v1/",
        model="deepseek-ai/DeepSeek-R1",
        proxies={
            "http://": "http://127.0.0.1:7890",
            "https://": "http://127.0.0.1:7890"
        }
    )
    tools = {
        "schedule_meeting": schedule_meeting,
        "get_weather": get_weather,
        "get_time": get_time,
        "send_email": send_email
    }

    # 示例 1：单轮对话（非流式，无额外参数）
    print("示例 1：单轮对话（非流式，无额外参数）")
    messages = [{"role": "user", "content": [{"type": "text", "text": "法国的首都是哪里？"}]}]
    async for part in api.chat(messages, stream=False):
        print(part, end="", flush=True)
    print("\n更新后的消息列表：", json.dumps(messages, ensure_ascii=False, indent=2))
    print()

    # 示例 2：多轮对话（非流式，无额外参数）
    print("示例 2：多轮对话（非流式，无额外参数）")
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "法国的首都是哪里？"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "法国的首都是巴黎。"}]},
        {"role": "user", "content": [{"type": "text", "text": "巴黎的人口是多少？"}]}
    ]
    async for part in api.chat(messages, stream=False):
        print(part, end="", flush=True)
    print("\n更新后的消息列表：", json.dumps(messages, ensure_ascii=False, indent=2))
    print()

    # 示例 3：单轮对话（流式，无额外参数）
    print("示例 3：单轮对话（流式，无额外参数）")
    messages = [{"role": "user", "content": [{"type": "text", "text": "讲一个关于魔法背包的故事。"}]}]
    async for part in api.chat(messages, stream=True):
        print(part, end="", flush=True)
    print("\n更新后的消息列表：", json.dumps(messages, ensure_ascii=False, indent=2))
    print()

    # 示例 4：多轮对话（流式，带工具和 presence_penalty）
    print("示例 4：多轮对话（流式，带工具和 presence_penalty）")
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "今天纽约的天气如何？"}]}
    ]
    async for part in api.chat(messages, stream=True, tools=tools, presence_penalty=0.5):
        print(part, end="", flush=True)
    print("\n更新后的消息列表：", json.dumps(messages, ensure_ascii=False, indent=2))
    print()

    # 示例 5：多个工具调用（流式，带工具）
    print("示例 5：多个工具调用（流式，带工具）")
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "请告诉我巴黎和波哥大的天气，并给 Bob 发送一封邮件（bob@email.com），内容为 'Hi Bob'。"}]}
    ]
    async for part in api.chat(messages, stream=True, tools=tools):
        print(part, end="", flush=True)
    print("\n更新后的消息列表：", json.dumps(messages, ensure_ascii=False, indent=2))
    print()

    # 示例 6：推理模式（流式，启用推理）
    print("示例 6：推理模式（流式，启用推理）")
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "你好"}]}
    ]
    async for part in api.chat(messages, stream=True, max_output_tokens=500):
        print(part, end="", flush=True)
    print("\n更新后的消息列表：", json.dumps(messages, ensure_ascii=False, indent=2))
    print()

    # 示例 7：结构化输出（非流式，使用 response_format）
    print("示例 7：结构化输出（非流式，使用 response_format）")
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "person_info",
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"}
                },
                "required": ["name", "age"],
                "additionalProperties": False
            },
            "strict": True
        }
    }
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "请提供一个人的信息，包括姓名和年龄。"}]}
    ]
    async for part in api.chat(messages, stream=False, response_format=response_format):
        print("结构化输出:", part, end="", flush=True)
    print("\n更新后的消息列表：", json.dumps(messages, ensure_ascii=False, indent=2))
    print()

    # 示例 8：聊天中使用多文件上传（PDF）
    print("示例 8：聊天中使用多文件上传（PDF）")
    
    file_paths = [
        'pdf1.pdf',
        'pdf2.pdf'
    ]
    display_names = ["doc1.pdf", "doc2.pdf"]

    # 上传文件
    upload_results = await api.upload_files(file_paths, display_names)
    file_parts = []
    for idx, result in enumerate(upload_results):
        if result["fileId"] and not result["error"]:
            file_parts.append({
                "input_file": {
                    "file_id": result["fileId"]
                }
            })
        else:
            print(f"文件 {file_paths[idx]} 上传失败: {result['error']}")

    if file_parts:
        # 构造包含 input_file 的聊天消息
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请总结以下 PDF 文件的要点："},
                *file_parts
            ]
        }]
        print("发送 PDF 文件进行聊天：")
        async for part in api.chat(messages, stream=False):
            print(part, end="", flush=True)
        print("\n更新后的消息列表：", json.dumps(messages, ensure_ascii=False, indent=2))
    else:
        print("无有效文件 ID，无法发起聊天")
    print()

    # 示例 9：聊天中使用多 inline 图片
    print("示例 9：聊天中使用多 inline 图片")
    
    file_paths = [
        '《Break the Cocoon》封面.jpg',
        '92D32EDFF4535D91F4E60234FD4703E1.jpg'
    ]

    # 转换为 inline 图片
    inline_results = await api.prepare_inline_image_batch(file_paths, detail="high")
    image_parts = []
    for idx, result in enumerate(inline_results):
        if "input_image" in result and result["input_image"]:
            image_parts.append({
                "input_image": result["input_image"]
            })
        else:
            print(f"图片 {file_paths[idx]} 处理失败: {result.get('error', '未知错误')}")

    if image_parts:
        # 构造包含 input_image 的聊天消息
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述以下图片的内容："},
                *image_parts
            ]
        }]
        print("发送 inline 图片进行聊天：")
        async for part in api.chat(messages, stream=False):
            print(part, end="", flush=True)
        print("\n更新后的消息列表：", json.dumps(messages, ensure_ascii=False, indent=2))
    else:
        print("无有效 inline 图片，无法发起聊天")
    print()

if __name__ == "__main__":
    asyncio.run(main())
