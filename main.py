# 导入AstrBot核心模块：Star基类（插件必须继承）、Context上下文（插件运行环境）
from astrbot.core.star import Star, Context
# 导入消息类型枚举：区分群消息/好友消息
from astrbot.core.platform import MessageType
# 导入指令注册装饰器：用于注册插件指令
from astrbot.core.star.register import register_command
# 导入MQTT客户端库：实现与 IoT平台的MQTT通信
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent

import paho.mqtt.client as mqtt
# 导入时间模块：用于时间戳
import time
# 导入JSON模块：处理MQTT消息的序列化/反序列化
import json
# 导入异步支持
import asyncio
# 导入类型注解：提升代码可读性和类型检查
from typing import Optional, Dict, Any

# ===================== 插件核心类 =====================
# 插件主类：必须继承AstrBot的Star基类
class CloudIoTSync(Star):
    # 初始化方法：AstrBot v4.x要求必须接收context和**kwargs参数
    def __init__(self, context: Context, config: AstrBotConfig, **kwargs):
        # 调用父类Star的初始化方法（v4.x核心要求）
        # 传入上下文、额外参数、默认配置
        # 注册默认配置到插件
        self.config = config
        super().__init__(
            context=context,
            **kwargs,
        )
        
        # ===================== MQTT客户端状态变量 =====================
        self.client: Optional[mqtt.Client] = None  # MQTT客户端实例
        self.connected = False  # MQTT连接状态标识（True=已连接）
        self.device_id = ""  # 设备ID（从client_id解析）
        self.topics: Dict[str, str] = {}  # MQTT主题字典（存储 标准主题）
        
        # 注册插件指令：初始化时自动注册所有/iot相关指令
        self._register_commands()
    
    # ===================== 指令注册 =====================
    def _register_commands(self):
        """注册插件所有指令（用户交互入口）"""
        
        # 指令1：连接 IoT平台
        @register_command(
            command_name="iot_connect",  # 指令唯一标识（内部使用）
            cmd="/iot_connect",  # 用户触发指令（群/私聊发送该内容）
            desc="连接 IoT平台\n用法：/iot connect",  # 指令描述（/plugin info查看）
            message_types=[MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE]  # 支持的消息类型
        )
        async def connect_iot(self, event: AstrMessageEvent):
            """处理/iot connect指令：初始化并连接MQTT客户端"""
            # 前置检查：已连接则直接返回提示
            if self.connected:
                yield event.plain_result(" (＾▽＾) 已连接到 IoT平台，无需重复连接")
                return
            
            try:
                # 初始化MQTT客户端（配置主题、认证、回调等）
                self._init_mqtt_client()
                # 建立MQTT连接 - 使用线程池避免阻塞事件循环
                await asyncio.to_thread(self._connect)
                # 等待连接确认（最多等待5秒）
                for _ in range(50):
                    if self.connected:
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise Exception("连接超时：未收到连接确认")
                # 连接确认后发送设备上线消息
                self.send_device_online()
                yield event.plain_result(
                    f" (＾▽＾) 成功连接到 IoT平台\n( ﾟ∀ﾟ) 服务器：{self.cfg('hostname')}:{self.cfg('port')}"
                )
            except Exception as e:
                # 异常处理：记录错误日志（含堆栈），回复用户失败原因
                logger.error(f"连接 IoT失败: {e}", exc_info=True)
                yield event.plain_result(f"(×_×) 连接失败：{str(e)}")
        
        # 指令2：断开 IoT平台连接
        @register_command(
            command_name="iot_disconnect",
            cmd="/iot_disconnect",
            desc="断开 IoT平台连接\n用法：/iot disconnect",
            message_types=[MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE]
        )
        async def disconnect_iot(self, event: AstrMessageEvent):
            """处理/iot disconnect指令：断开MQTT连接"""
            # 前置检查：未连接则直接返回提示
            if not self.connected and self.client is None:
                yield event.plain_result("(×_×) 尚未连接到 IoT平台")
                return
            
            try:
                # 执行断开连接逻辑 - 使用线程池避免阻塞事件循环
                await asyncio.to_thread(self._disconnect)
                # 回复用户：断开成功
                yield event.plain_result(" (＾▽＾) 已断开与 IoT平台的连接")
            except Exception as e:
                logger.error(f"断开连接失败: {e}", exc_info=True)
                yield event.plain_result(f"(×_×) 断开失败：{str(e)}")
        
        # 指令3：手动上报设备属性
        @register_command(
            command_name="iot_report",
            cmd="/iot_report",
            desc="手动上报设备属性\n用法：/iot report <属性类型> <值>\n示例：/iot_report temperature 25",
            message_types=[MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE]
        )
        async def report_property(self, event: AstrMessageEvent):
            """处理/iot report指令：手动上报指定类型的设备属性"""
            if not self.connected:
                yield event.plain_result("(×_×) 尚未连接到 IoT平台，请先执行 /iot_connect")
                return
            
            # 直接从event获取消息内容
            message_parts = event.message_str.split()
            if len(message_parts) < 2:  # 检查参数数量：/iot_report <属性类型> <值>
                yield event.plain_result("(×_×) 参数错误！\n正确用法：/iot report <属性类型> <值>\n示例：/iot_report temperature 25")
                return
            
            # 提取参数：属性类型（小写）、属性值
            # message_parts[0]="/iot_report", message_parts[1]=属性类型
            prop_type = message_parts[1].lower()
            value = message_parts[2] if len(message_parts) > 2 else ""
            
            if not value:
                yield event.plain_result("(×_×) 缺少属性值！")
                return
            
            try:
                await asyncio.to_thread(self.send_device_property, "Custom", {prop_type: value})
                
                yield event.plain_result(f" (＾▽＾) 已上报属性\n(⊙_⊙) 类型：{prop_type}\n(⇀‸↼) 值：{value}")
            except Exception as e:
                logger.error(f"上报属性失败: {e}", exc_info=True)
                yield event.plain_result(f"(×_×) 上报失败：{str(e)}")
        
        # 指令4：发送设备事件到平台
        @register_command(
            command_name="iot_event",
            cmd="/iot_event",
            desc="发送设备事件到平台\n用法：/iot_event <事件类型> <消息>\n示例：/iot_event alert 温度过高",
            message_types=[MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE]
        )
        async def send_event(self, event: AstrMessageEvent):
            """处理/iot_event指令：发送自定义事件到华为云IoT平台"""
            if not self.connected:
                yield event.plain_result("(×_×) 尚未连接到华为云IoT平台，请先执行 /iot_connect")
                return
            
            # 直接从event获取消息内容
            message_parts = event.message_str.split(maxsplit=2)  # 限制分割次数
            if len(message_parts) < 2:
                yield event.plain_result("(×_×) 参数错误！\n正确用法：/iot_event <事件类型> <消息>\n示例：/iot_event alert 温度过高")
                return
            
            # 提取参数
            # message_parts[0]="/iot_event", message_parts[1]=事件类型
            event_type = message_parts[1]
            event_msg = message_parts[2] if len(message_parts) > 2 else ""
            
            if not event_msg:
                yield event.plain_result("(×_×) 缺少事件消息！")
                return
            
            try:
                await asyncio.to_thread(
                    self.send_device_event,
                    event_type, 
                    {"message": event_msg}
                )
                yield event.plain_result(f" (＾▽＾) 已发送事件\n ( ͡° ͜ʖ ͡°) 类型：{event_type}\n(´･ω･`)消息：{event_msg}")
            except Exception as e:
                logger.error(f"发送事件失败: {e}", exc_info=True)
                yield event.plain_result(f"(×_×) 发送失败：{str(e)}")
        
        # 指令5：查看MQTT连接状态
        @register_command(
            command_name="iot_status",
            cmd="/iot_status",
            desc="查看 IoT平台连接状态\n用法：/iot_status",
            message_types=[MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE]
        )
        async def check_status(self, event: AstrMessageEvent):
            """处理/iot status指令：返回当前MQTT连接状态及关键配置"""
            # 构造连接状态文本
            status = " (＾▽＾) 已连接" if self.connected else "(×_×) 未连接"
            # 构造状态信息（多行格式化）
            info = f"""( ﾟ∀ﾟ)  IoT平台状态
            {status}
            (^_^) 服务器：{self.cfg('hostname')}:{self.cfg('port')}
             (o_O) 设备ID：{self.device_id}
            """
            # 回复用户状态信息
            yield event.plain_result(info)
        
        # 指令6：自定义Topic消息发送（透传）
        @register_command(
            command_name="iot_pub",
            cmd="/iot_pub",
            desc="发送普通MQTT消息（自定义Topic）\n用法：/iot_pub <topic> <消息内容>\n示例：/iot_pub test/topic hello",
            message_types=[MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE]
        )
        async def publish_raw_message(self, event: AstrMessageEvent):
            """处理/iot pub指令：发送普通MQTT消息（非属性/事件）"""

            if not self.connected:
                yield event.plain_result("(×_×) 尚未连接到 IoT平台，请先执行 /iot connect")
                return

            # 拆分指令（最多拆3段，保留完整消息）
            # parts[0]="/iot_pub",parts[1]=topic, parts[2]=消息内容
            parts = event.message_str.split(maxsplit=2)

            if len(parts) < 2:
                yield event.plain_result(
                    "(×_×) 参数错误！\n用法：/iot pub <topic> <消息内容>\n示例：/iot pub_test/topic hello"
                )
                return

            topic = parts[1]
            message = parts[2]

            try:
                await asyncio.to_thread(self.send_raw_message, topic, message)
                yield event.plain_result(
                    f" (＾▽＾) 消息已发送\n( ﾟ∀ﾟ) Topic：{topic}\n(´･ω･`)内容：{message}"
                )
            except Exception as e:
                logger.error(f"发送普通消息失败: {e}", exc_info=True)
                yield event.plain_result(f"(×_×) 发送失败：{str(e)}")
    
    # ===================== MQTT客户端核心逻辑 =====================
    def _init_mqtt_client(self):
        """初始化MQTT客户端：配置主题、认证、回调函数、遗嘱消息等"""
        # 解析设备ID：从client_id中提取前缀（ IoT设备唯一标识）
        self.device_id = self.cfg('client_id').split('_')[0]
        
        # 初始化 IoT平台标准MQTT主题（按平台规范定义）
        self.topics = {
            # 属性上报主题：设备→平台
            "property_up": f"$oc/devices/{self.device_id}/sys/properties/report",
            # 事件上报主题：设备→平台（上报告警、状态等事件）
            "event_up": f"$oc/devices/{self.device_id}/sys/events/up",
            # 命令接收主题：平台→设备（订阅平台下发的指令）
            "command_down": f"$oc/devices/{self.device_id}/sys/commands/#",
            # 属性设置主题：平台→设备（订阅平台设置的属性）
            "property_set": f"$oc/devices/{self.device_id}/sys/properties/set/#",
            # 属性获取响应主题：设备→平台（回复平台的属性查询）
            "property_get_response": f"$oc/devices/{self.device_id}/sys/properties/get/response/+"
        }
        
        # 创建MQTT客户端实例（v5版本，兼容 IoT平台）
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,  # 使用v2回调API（paho-mqtt 2.0+要求）
            client_id=self.cfg('client_id'),  # 设备client_id
            protocol=mqtt.MQTTv5,  # MQTT协议版本（v5）
            transport="tcp"  # 传输层协议（TCP）
        )
        
        # 设置MQTT回调函数：连接/消息接收/断开连接时触发
        self.client.on_connect = self._on_connect  # 连接成功/失败回调
        self.client.on_message = self._on_message  # 收到消息回调
        self.client.on_disconnect = self._on_disconnect  # 断开连接回调
        
        # 设置MQTT认证信息：用户名+密码（ IoT平台设备认证）
        self.client.username_pw_set(
            self.cfg('username'),  # 设备用户名
            self.cfg('password')   # 设备密码
        )
        
        # 设置遗嘱消息（Last Will）：设备异常断开时，平台自动收到该消息
        will_topic = f"$oc/devices/{self.device_id}/sys/messages/down"  # 遗嘱主题
        will_payload = json.dumps({  # 遗嘱内容（JSON格式）
            "message_type": "device_offline",  # 消息类型：设备离线
            "device_id": self.device_id,       # 设备ID
            "timestamp": int(time.time() * 1000)  # 离线时间戳（毫秒）
        })
        # 配置遗嘱消息：QoS=1（至少一次送达），不保留
        self.client.will_set(will_topic, will_payload, qos=1, retain=False)
    
    def send_raw_message(self, topic: str, message: str):
        """发送普通MQTT消息（透传，不走IoT规范）

        Args:
            topic: 自定义Topic
            message: 消息内容（字符串或JSON字符串）
        """
        if not self.connected:
            raise Exception("未连接到 IoT平台")

        # 如果是JSON字符串，尝试格式化（可选）
        try:
            payload = json.dumps(json.loads(message))
        except json.JSONDecodeError:
            payload = message  # 普通字符串直接发送
        except Exception as e:
            logger.error(f"处理消息内容时出错: {e}", exc_info=True)
            payload = message  #  fallback到原始消息

        self.client.publish(topic, payload, qos=1)
        logger.info(f"发送普通消息 -> Topic: {topic}, Payload: {payload}")
    
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """MQTT连接回调函数：连接成功/失败时触发"""
        # 连接成功（reason_code=0表示成功）
        if reason_code == 0:
            self.connected = True  # 更新连接状态为已连接
            logger.info(f"成功连接到 IoT平台：{self.cfg('hostname')}:{self.cfg('port')}")
            
            # 订阅所有预定义的MQTT主题（接收平台下发的指令/属性）
            for topic_name, topic_path in self.topics.items():
                try:
                    client.subscribe(topic_path, qos=1)  # QoS=1（至少一次送达）
                    logger.info(f"订阅主题成功：{topic_name} -> {topic_path}")
                except Exception as e:
                    logger.error(f"订阅主题失败 {topic_name}：{e}", exc_info=True)
            
            # 发送设备上线消息：告知平台设备已在线
        # 连接失败
        else:
            self.connected = False  # 更新连接状态为未连接
            logger.error(f"连接失败，原因码：{reason_code}")
            # 原因码5：认证失败（用户名/密码/client_id错误）
            if reason_code == 5:
                logger.error("认证失败，请检查用户名和密码是否正确！")
    
    def _on_message(self, client, userdata, msg):
        """MQTT消息接收回调函数：收到平台下发的消息时触发"""
        try:
            # 解码消息体：字节→字符串
            payload_str = msg.payload.decode('utf-8')
            # 解析JSON格式消息
            payload_json = json.loads(payload_str)
            # 记录日志：格式化输出消息内容（便于调试）
            logger.info(f"收到MQTT消息 [主题: {msg.topic}]: {json.dumps(payload_json, indent=2, ensure_ascii=False)}")
            
            # 处理平台下发的消息（指令/属性设置等）
            self._handle_platform_message(msg.topic, payload_json)
            
        # 非JSON格式消息处理
        except json.JSONDecodeError:
            logger.info(f"收到非JSON消息 [主题: {msg.topic}]: {msg.payload.decode('utf-8')}")
        # 其他异常处理
        except Exception as e:
            logger.error(f"处理MQTT消息出错：{e}", exc_info=True)
    
    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """MQTT断开连接回调函数：连接断开时触发"""
        self.connected = False  # 更新连接状态为未连接
        logger.info(f"与 IoT平台断开连接，原因码：{reason_code}")
    
    def _handle_platform_message(self, topic: str, payload: Dict[str, Any]):
        """处理平台下发的消息：指令/属性设置等"""
        # 场景1：处理平台下发的命令（如远程控制指令）
        if "commands" in topic:
            logger.info("处理平台下发命令")
            # 提取命令ID（用于回复平台）
            command_id = payload.get("command_id", "unknown")
            # 构造命令响应主题：将request替换为response，并保留命令ID
            # 实际topic格式通常为：$oc/devices/{device_id}/sys/commands/request/{request_id}
            response_topic = topic.replace("/request/", f"/response/{command_id}/")
            # 备用方案：如果上面替换不生效，使用更通用的方式
            if response_topic == topic:
                # 从topic中提取基础路径并构造响应主题
                topic_parts = topic.split("/")
                # 查找"request"的位置
                for i, part in enumerate(topic_parts):
                    if part == "request":
                        topic_parts[i] = "response"
                        # 在response后插入command_id
                        if i + 1 < len(topic_parts):
                            topic_parts[i + 1] = command_id
                        break
                response_topic = "/".join(topic_parts)
            
            # 构建命令响应数据（按 格式）
            response_data = {
                "result_code": 0,  # 0=成功，非0=失败
                "response_name": "COMMAND_RESPONSE",  # 响应类型
                "paras": {"result": "success", "message": "AstrBot已处理命令"}  # 响应内容
            }
            
            # 发布响应消息到平台
            self.client.publish(response_topic, json.dumps(response_data), qos=1)
            logger.info(f"已发送命令响应到：{response_topic}")
        
        # 场景2：处理平台下发的属性设置（如远程设置设备参数）
        elif "properties/set" in topic:
            logger.info("处理平台属性设置")
            # 提取属性设置的服务列表
            services = payload.get("services", [])
            for service in services:
                # 提取服务ID和属性键值对
                service_id = service.get("service_id", "unknown")
                properties = service.get("properties", {})
                logger.info(f"平台设置属性 - 服务：{service_id}，属性：{properties}")
            
            # 构建属性设置响应数据
            response_data = {
                "result_code": 0,  # 0=成功
                "response_name": "SET_RESPONSE"  # 响应类型：属性设置响应
            }
            # 发布响应到属性上报主题
            self.client.publish(self.topics["property_up"], json.dumps(response_data), qos=1)
    
    # ===================== MQTT连接管理 =====================
    def _connect(self):
        """建立MQTT连接：启动网络循环并连接服务器"""
        # 前置检查：客户端未初始化则先初始化
        if self.client is None:
            self._init_mqtt_client()
        
        # 连接 IoT平台MQTT服务器
        self.client.connect(
            self.cfg('hostname'),  # 服务器地址
            self.cfg('port'),      # 端口号
        )
        
        # 启动MQTT网络循环（后台线程）：处理消息收发
        self.client.loop_start()
    
    def _disconnect(self):
        """断开MQTT连接：停止网络循环并清理资源"""
        # 确保网络循环和连接都被清理，即使连接未完全建立
        if self.client:
            try:
                # 停止MQTT网络循环（必须先停止，否则可能残留线程）
                self.client.loop_stop()
            except Exception as e:
                logger.warning(f"停止MQTT循环时出现警告: {e}")
            try:
                # 断开MQTT连接
                self.client.disconnect()
            except Exception as e:
                logger.warning(f"断开MQTT连接时出现警告: {e}")
            # 更新连接状态
            self.connected = False
            self.client = None
    
    # ===================== 消息发布（设备→平台） =====================
    def send_device_online(self):
        """发送设备上线消息：告知平台设备已在线"""
        # 构造上线消息（按 IoT属性上报格式）
        online_message = {
            "services": [{  # 服务列表（ IoT规范）
                "service_id": "DeviceInfo",  # 服务ID：设备信息
                "properties": {  # 属性键值对
                    "status": "online",               # 设备状态：在线
                    "device_type": "AstrBot",         # 设备类型：AstrBot机器人
                    "firmware_version": "1.0.0",      # 固件版本
                    "connect_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # 连接时间（UTC）
                }
            }]
        }
        
        # 发布上线消息到属性上报主题
        self.client.publish(self.topics["property_up"], json.dumps(online_message), qos=1)
        logger.info("发送设备上线消息")
    
    def send_device_property(self, service_id: str, properties: Dict[str, Any]):
        """发送设备属性到平台
        
        Args:
            service_id: 服务ID（如Temperature/Humidity/Custom）
            properties: 属性键值对（如{"value": 25, "unit": "Celsius"}）
        """
        # 前置检查：未连接则抛出异常
        if not self.connected:
            raise Exception("未连接到 IoT平台")
        
        # 构造属性上报消息（按 格式）
        property_message = {
            "services": [{
                "service_id": service_id,  # 服务ID
                "properties": properties   # 属性内容
            }]
        }
        
        # 发布属性消息到属性上报主题
        self.client.publish(self.topics["property_up"], json.dumps(property_message), qos=1)
        logger.info(f"发送设备属性 - 服务：{service_id}，属性：{properties}")
    
    def send_device_event(self, event_type: str, event_data: Dict[str, Any]):
        """发送设备事件到平台
        
        Args:
            event_type: 事件类型（如alert/device_status）
            event_data: 事件数据（如{"message": "温度过高", "timestamp": 1710988800000}）
        """
        # 前置检查：未连接则抛出异常
        if not self.connected:
            raise Exception("未连接到 IoT平台")
        
        # 构造事件上报消息（按 格式）
        event_message = {
            "events": [{  # 事件列表
                "event_type": event_type,  # 事件类型
                "event_data": event_data,  # 事件数据
                "timestamp": int(time.time() * 1000)  # 事件时间戳（毫秒）
            }]
        }
        
        # 发布事件消息到事件上报主题
        self.client.publish(self.topics["event_up"], json.dumps(event_message), qos=1)
        logger.info(f"发送设备事件 - 类型：{event_type}，数据：{event_data}")
    

    
    # ===================== 插件生命周期 =====================
    async def terminate(self):
        """插件卸载时的资源清理：断开连接（AstrBot v4.x要求）"""
        if self.client:
            await asyncio.to_thread(self._disconnect)  # 断开MQTT连接
        logger.info(" IoT插件已卸载，资源已清理")
    
    async def handle(self, ctx: Context):
        """插件核心处理入口（AstrBot v4.x 必须实现）
        指令由装饰器自动路由，此处无需额外处理
        """
        pass
    
    def cfg(self, key, default=None):
        item = self.config.get(key)

        if isinstance(item, dict):
            val = item.get("value", item.get("default", default))
        else:
            val = item or default
        return val

# ===================== 插件入口 =====================
def load_star():
    """插件加载入口（AstrBot启动时自动调用）
    返回插件主类，供框架初始化实例
    """
    return CloudIoTSync
