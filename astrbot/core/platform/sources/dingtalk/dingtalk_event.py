import asyncio
from typing import cast, Any

import dingtalk_stream

import astrbot.api.message_components as Comp
from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain


class DingtalkMessageEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str,
        message_obj,
        platform_meta,
        session_id,
        client: dingtalk_stream.ChatbotHandler,
        adapter: "Any" = None,
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client = client
        self.adapter = adapter

    async def send_with_client(
        self,
        client: dingtalk_stream.ChatbotHandler,
        message: MessageChain,
    ):
        icm = cast(dingtalk_stream.ChatbotMessage, self.message_obj.raw_message)
        ats = []
        # fixes: #4218
        # 钉钉 at 机器人需要使用 sender_staff_id 而不是 sender_id
        for i in message.chain:
            if isinstance(i, Comp.At):
                print(i.qq, icm.sender_id, icm.sender_staff_id)
                if str(i.qq) in str(icm.sender_id or ""):
                    # 适配器会将开头的 $:LWCP_v1:$ 去掉，因此我们用 in 判断
                    ats.append(f"@{icm.sender_staff_id}")
                else:
                    ats.append(f"@{i.qq}")
        at_str = " ".join(ats)

        for segment in message.chain:
            if isinstance(segment, Comp.Plain):
                segment.text = segment.text.strip()
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    client.reply_markdown,
                    segment.text,
                    f"{at_str} {segment.text}".strip(),
                    cast(dingtalk_stream.ChatbotMessage, self.message_obj.raw_message),
                )
            elif isinstance(segment, Comp.Image):
                markdown_str = ""

                try:
                    if not segment.file:
                        logger.warning("钉钉图片 segment 缺少 file 字段，跳过")
                        continue
                    if segment.file.startswith(("http://", "https://")):
                        image_url = segment.file
                    else:
                        image_url = await segment.register_to_file_service()

                    markdown_str = f"![image]({image_url})\n\n"

                    ret = await asyncio.get_event_loop().run_in_executor(
                        None,
                        client.reply_markdown,
                        "😄",
                        markdown_str,
                        cast(
                            dingtalk_stream.ChatbotMessage, self.message_obj.raw_message
                        ),
                    )
                    logger.debug(f"send image: {ret}")

                except Exception as e:
                    logger.warning(f"钉钉图片处理失败: {e}, 跳过图片发送")
                    continue

    async def send(self, message: MessageChain):
        await self.send_with_client(self.client, message)
        await super().send(message)

    async def _fallback_to_text(self, generator):
        buffer = None
        async for chain in generator:
            if not buffer:
                buffer = chain
            else:
                buffer.chain.extend(chain.chain)
        
        if buffer:
            buffer.squash_plain()
            await self.send(buffer)

    async def _send_card_streaming(self, generator, msg_id: str):
        full_content = ""
        seq = 0
        try:
            async for chain in generator:
                for segment in chain.chain:
                    if isinstance(segment, Comp.Plain):
                        full_content += segment.text
                    elif isinstance(segment, Comp.At):
                        icm = cast(dingtalk_stream.ChatbotMessage, self.message_obj.raw_message)
                        if str(segment.qq) in str(icm.sender_id or ""):
                            full_content += f"@{icm.sender_staff_id} "
                        else:
                            full_content += f"@{segment.qq} "
                    elif isinstance(segment, Comp.Image):
                        if segment.file:
                            if segment.file.startswith(("http://", "https://")):
                                image_url = segment.file
                            else:
                                image_url = await segment.register_to_file_service()
                            full_content += f"![image]({image_url})\n"
                
                seq += 1
                if seq % 2 == 0: # Update every 2 chunks to be more responsive than 8
                    await self.adapter.send_card_message(msg_id, full_content, is_final=False)
            
            await self.adapter.send_card_message(msg_id, full_content, is_final=True)
        except Exception as e:
            logger.error(f"DingTalk streaming error: {e}")
            # Try to ensure final state is sent or cleaned up?
            await self.adapter.send_card_message(msg_id, full_content, is_final=True)

    async def _init_card_streaming(self) -> bool:
        if not self.adapter or not self.adapter.card_template_id:
            logger.warning(f"DingTalk streaming is enabled, but 'card_template_id' is not configured for platform '{self.platform_meta.id}'. Falling back to text streaming.")
            return False

        msg_id = self.message_obj.message_id
        incoming_msg = self.message_obj.raw_message
        return await self.adapter.create_message_card(msg_id, incoming_msg)

    async def send_streaming(self, generator, use_fallback: bool = False):
        if await self._init_card_streaming():
            await self._send_card_streaming(generator, self.message_obj.message_id)
        else:
            await self._fallback_to_text(generator)

