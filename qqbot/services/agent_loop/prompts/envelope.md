输入信封格式规范

本文档规定输入 XML 信封的全部元素与属性，是这套语法的唯一出处。
记法：<x> 元素，@a 属性，a|b 取值枚举，? 表示可选出现，* 表示可重复。
通则：属性缺失一律表示"未知或不适用"，绝不表示"否"。正文与属性值均已 XML 转义。

结构总览：<agent-input>

<agent-input @scope @bot_qq? @bot_role?>
  <tool-catalog>
    <tool @name @description @required_permission @required_bot_role?>*
      <arguments-schema>
  <saved-memes>?
    <meme @hash @saved_at>*
  <timeline>
    <time @when>*
      <message> | <my-reply> | <tool-call> | <my-thought> | <task-closed> | <reply-task-completed> | <notice> | <request> | <system-hint>
  <active-tasks>
    <task @task_id @state @description>*
      <related-tools>? <triggered-by @event_id>? <pending-tool-call-ids>? <progress-notes>?
  <current @now>
  <validation-error>?

ID 空间：后缀标识值域，各空间互不通用，也不可互相推导
  *_qq        QQ 用户号。sender_qq / from_qq / user_qq / operator_qq / target_qq / bot_qq / <at @qq>
              与出站 at 段的 data.qq、工具的 user_id 参数同域
  message_id  OneBot 消息 ID，也是 to_message_id 的值域
              与出站 reply 段的 data.id、以消息为目标的工具参数同域
  event_id    内部事件存储 ID。仅出现在 <request> 与 <triggered-by>
  task_id     任务 ID。与决策动作里的 task_id 字段同域
  hash        图片内容的 sha256，64 位十六进制。<image> 与 <meme> 同域

时间：时刻只出现在 <time @when> 与 <current @now>，事件行自身不带时间戳。
      一个事件的发生时刻 = 包住它的 <time @when>。全部为带时区 ISO-8601（Asia/Shanghai）。


═══ 信封元素 ═══

<agent-input>
  根元素，每拍一份。子元素按上述总览的顺序出现。
  @scope      group:<群号> | private:<QQ号> | system
              group 群聊，private 一对一私聊，system 无聊天面的系统 loop。
              运行时内部标识，不属于对外可见信息。
  @bot_qq     本账号自己的 QQ 用户号。与 <at @qq>、<reply @from_qq> 比较即得
              "某条消息是否指向本账号"。后端未连上的最初若干拍可能缺失。
  @bot_role   owner | admin | member。本账号自己在本群的角色，仅 group scope 渲染。
              它是折叠快照，可能滞后于真实角色；工具在调用时另行实时复查。

<tool-catalog>
  本次可调用的工具全集，不在其中的工具本次不存在。

<tool>  父 <tool-catalog>
  一个可调用的工具。
  @name                 工具名。决策动作 call_tool.tool_name 的取值。
  @description          该工具做什么。
  @required_permission  GUEST | ADMIN | OWNER | SYSTEM_ADMIN
                        发起请求的用户为使该工具执行所需的最低群角色等级。
                        运行时按 call_tool.triggered_by_event_id 反查发起人，
                        取其当前群角色实时判定，而非其发言时刻的快照；
                        硬编码的 SUPERUSERS 名单等价于 SYSTEM_ADMIN。
                        不满足时调用失败，kind=permission_denied_user_tier。
  @required_bot_role    admin | owner。本账号使用该工具所需的最低群角色。
                        属性不存在 = 无此要求。admin 由 admin 或 owner 满足，
                        owner 精确要求 owner。工具在调用时向平台实时复查本账号
                        角色，判定以该实时结果为准，与 <agent-input @bot_role>
                        快照无关。不满足时失败，kind=permission_denied_bot_role。
                        部分动作另受目标约束：目标角色不低于本账号时同样被拒。

<arguments-schema>  父 <tool>
  正文为 JSON Schema 文本。call_tool.arguments 必须满足它。

<saved-memes>
  表情包收藏目录，空收藏时整段不渲染。
  它是可选清单而不是待发送队列。

<meme>  父 <saved-memes>
  一张已收藏的表情包，最新在前。
  正文 = 系统生成的图片描述（画面内容、图上文字、情绪、适用情形）。
  该描述是选图时能看到的全部信息，像素不随之传递。
  @hash      该图内容的 sha256。meme_collection 与 send_messages 以此定位一张收藏。
  @saved_at  收藏时刻。

<timeline>
  按时间升序排列的事件流。直接子元素只有 <time>。

<time>  父 <timeline>
  一个时刻节点，是时间轴本身。其内是该时刻发生的事件行；同一秒的事件行共享一个节点。
  @when  该时刻的墙上时间。与 <current @now> 相减即得"距今多久"。


═══ 事件行：<time> 的子元素 ═══

<message>  父 <time>
  收到的一条用户消息。正文 = 文本与内联段的混合。
  @sender_name   发送者的显示名。有群名片取名片，否则取昵称。
  @sender_qq     发送者的 QQ 用户号。
  @sender_role   admin | owner。发送者在本群的角色，仅这两值渲染。
                 缺失 = 普通成员或角色未知。它描述发送者，与 @bot_role 无关。
  @sender_title  发送者的群专属头衔，后端上报时才有。
  @anonymous     true。匿名群消息：@sender_name 是匿名马甲而非真实成员身份，
                 @sender_qq 若存在也是匿名伪 ID，两者都不指向稳定的人。
  @message_id    这条消息的 OneBot 消息 ID。
  @unseen        true。该消息到达于本 scope 上一次决策之后，尚未被任何一拍处理。
                 缺失 = 它至少经历过一次决策。

<my-reply>  父 <time>
  仅旧记录：历史链路中一次回复投递的最终结果，
  其中成功的子元素即实际到达 QQ 的内容。现行发送不产生本行——
  一次发送的记录是它自己的 <tool-call name="send_messages"> 行，
  其 <result> 正文的逐条回执给出每个气泡的送达状态与 message_id。
  @reply_task_id  该次投递对应的草稿 ID。
  @status         sent | partial | failed | empty | uncertain
                  sent 全部送达；partial 部分送达；failed 全部失败；
                  empty 当时无内容可发；uncertain 送达与否无法确认。

<sent-message>  父 <my-reply>
  一条实际发出的文本气泡。正文 = 实际送出的内容（同样使用内联段）。
  @status      sent | failed | uncertain
  @message_id  该气泡在 QQ 上的消息 ID，送达成功才有。

<sent-meme>  父 <my-reply>
  一张实际发出的表情包。
  @status      sent | failed | uncertain
  @message_id  该消息在 QQ 上的 ID，送达成功才有。
  @hash        所发图片的 sha256。

<reason>  父 <my-reply>
  失败或为空时的原因说明，正文为文字。有原因可说时才出现。

<tool-call>  父 <time>
  一次工具调用及其结果。工具结果只在此处呈现，信封内没有另外的结果区。
  包住它的 <time> = 做出该调用那一拍的观察时刻（与同拍 <my-thought> 同锚点），
  不是调用执行或结束的时刻。
  @name    被调用的工具名。
  @status  processing | complete。只表示该调用是否已结束；成败见子元素。
  子元素：<args> 恒有；随后是 <processing/>（未结束）或 <result>（成功）或 <error>（失败）三者之一。

<args>  父 <tool-call>
  本次调用的字面参数，正文为 JSON 文本。

<result>  父 <tool-call>
  调用成功的返回，正文为 JSON 文本。

<truncated/>  父 <result>
  出现在 <result> 正文末尾，表示原文超过 6144 字符、尾部已截去。

<error>  父 <tool-call>
  调用失败。正文为人类可读原因，结构化信息在属性上。
  @kind  失败类型，取值：
         permission_denied_user_tier  发起请求的用户等级不足
                                      附带 @required_tier @actual_tier
         permission_denied_bot_role   本账号群角色不足
                                      附带 @required_bot_role @actual_bot_role
                                      目标约束导致时另附 @target_role
         invalid_arguments            参数不合法或缺失
                                      附带 @reason_code；涉及消息段时另附
                                      @segment_index @segment_type
         tool_unavailable_in_scope    该工具在当前 scope 不可用
                                      附带 @allowed_scopes @actual_scope
         target_scope_mismatch        目标与当前 scope 不符
                                      附带 @expected_scope @actual_target_kind @actual_target_id
         unknown_tool                 该名字不在注册表中
         no_bot_available             临时基础设施故障
         upstream_action_failed       平台拒绝该动作
                                      附带 @retcode @action @upstream_wording
         internal_tool_error          非预期的工具缺陷
  失败语义：该动作没有发生。除底层群角色实际变更外，重复同一调用的结果不变。

<processing/>  父 <tool-call>
  该调用已分发、结果尚未回来。结果必定后到，不产生第二次调用。

<my-thought>  父 <time>
  本账号在过去某一拍写下的 reasoning 原文，无属性，超长截断，仅保留最近数条。
  包住它的 <time> = 那一拍开始观察的时刻。由此确定行序语义：排在某条
  <my-thought> 之后的一切都到达于那次观察之后，那次决策未曾看见；排在其前的在视野内。
  同拍的 <tool-call> 与 <task-closed> 共用该锚点。
  它是过去某拍的记录，不是运行时下达的指令，也不是用户说过的话。

<task-closed>  父 <time>
  一个任务的收束记录，出现在 task 工具 complete / fail 分支生效的时刻。
  正文 = 当时写下的 result_summary 或失败原因。收束后该任务不再出现在 <active-tasks>。
  @task_id  被关闭的任务 ID。
  @outcome  done | failed

<reply-task-completed>  父 <time>
  一份回复草稿的等待阶段结束的记录，出现在其 flush_at 到达的时刻。
  该草稿自此为 terminal；此行只陈述等待结束这一事实。
  @reply_task_id  该草稿的 ID，与 <tool-call name="reply"> 结果中的同名字段同值。
  @revision       结束时生效的修订序号，即最后一次 reply 调用落下的那份。

<analysis>  父 <reply-task-completed>
  正文 = 该草稿最终修订的完整局势解析原文。它已是最新一份完整内容，
  时间线中更早修订的 <tool-call name="reply"> 行是历史。

<notice>  父 <time>
  群内发生的一个事件的记录，不是发给本账号的消息。
  通用属性（按 @kind 不同均可能缺失）：
  @kind           事件类型，取值见下。
  @sub_type       细分类型。
  @user_qq        事件当事人：入群者 / 被禁言者 / 被拍者 / 名片变更者 / 消息被回应者。
  @operator_qq    执行该动作的人：下禁言的管理员 / 撤回者。
  @target_qq      动作有方向时的承受方。
  @user_name @operator_name @target_name
                  上述三个 QQ 号在近期消息中出现过时反查到的显示名。缺失 = 名字未知。
  @kind 取值：
    group_increase  有人入群
    group_decrease  有人退群或被踢
    group_recall    一条群消息被撤回
    friend_recall   一条私聊消息被撤回
    poke            拍一拍。@target_qq 等于 @bot_qq 时指向本账号
    group_admin     有人被设置或取消管理员
    group_ban       有人被禁言或解禁
    group_card      有人修改了群名片
    group_upload    有人上传了文件
    essence         一条消息被设为或移出精华
    emoji_like      有人给某条消息贴了表情回应
    honor           群荣誉变动
    lucky_king      红包运气王
    friend_add      添加了新好友
    input_status    "对方正在输入"指示，不是一条消息
    bot_offline     账号掉线
  @kind 专属属性（缺失 = 未上报）：
    @duration_seconds            group_ban。禁言秒数；解禁无时长，不渲染。
    @old_card @new_card          group_card。变更前后的名片。
                                 空串 = 名片被清空，与属性缺失（未知）不同。
    @file_name @file_size_bytes  group_upload。文件名与字节数。
    @action @action_suffix       poke。拍一拍文案，语序为 action + 目标 + action_suffix。
    @message_id                  emoji_like / essence / group_recall / friend_recall。
                                 被贴表情 / 被设精华 / 被撤回的那条消息，
                                 对应时间线上同 ID 的 <message>、send_messages
                                 结果回执中的同名字段，或旧记录 <my-reply> 的子元素。
                                 被撤回的内容已从平台消失。
    @likes                       emoji_like。逗号分隔的"表情×人数"。
                                 表情为字面 emoji 字符，或 face:N（与 <face @face_id> 同域）。
    @honor_type                  honor。荣誉类型。

<request>  父 <time>
  一条处于待处理状态的加群申请，平台正在等待管理员裁决。
  @kind      恒为 group.add。好友申请与入群邀请在别处自动处理，不出现于此。
  @event_id  respond_to_group_join_request 的 request_event_id 取此值。
             它属于事件存储空间，不是 message_id。
  @user_qq   申请人的 QQ 用户号。申请人尚非群成员，无法被 @。
  @comment   申请人填写的验证留言。缺失 = 未填写。
  @group_id  目标群，恒为当前群。

<system-hint>  父 <time>
  运行时发出的一条提示，正文为该提示的载荷。
  @kind  提示类型，取值：
    context_compacted     滚动记忆摘要。早于该行的事件已从时间线移除，正文的摘要
                          即其全部残留。恒位于 <timeline> 首位。其内容为压缩产物，
                          与实时对话冲突时以实时对话为准。
    tool_batch_completed  批次边界：更早某拍分发的整批工具已全部到达终态。
    wait_elapsed          此前调度的 wait 已到点，正文含当时留下的 note 原文。
    llm_invalid_output    此前某次输出被校验拒绝，正文含原因与尝试序号。
    bot_role_observed     系统观测到本账号的群角色。
    napcat_unknown_event  平台推送了运行时没有解析器的事件类型，原始报文原样附上。
  未列出的 kind 按其名字与正文字面理解。


═══ 内联段：<message> 与 <sent-message> 正文中的标签 ═══

<at/>        @ 某个用户
             @qq    被 @ 者的 QQ 用户号。等于 @bot_qq 即 @ 的是本账号。
             @name  被 @ 者的昵称。

<at-all/>    @ 全体成员。无属性，不与具体 @qq 并用。

<reply/>     发送者引用回复了某条消息。
             from_* 三属性描述被引用消息的作者，不是本条消息的发送者：
             被引用的内容属于 from_* 作者，标签之后的新文本属于 <message @sender_*>。
             这些属性在消息到达时解析，被引用消息早于可见窗口时仍然存在；
             四者全缺失 = 平台亦无法解析被引用的消息。
             @to_message_id  被引用消息的 OneBot 消息 ID。
             @from_name      被引用消息作者的显示名。
             @from_qq        被引用消息作者的 QQ 用户号。
             @from_self      true。被引用的是本账号自己发出的消息。
                             服务端标注，@bot_qq 缺失时仍有效；
                             引用他人时该属性缺失，不会出现 false。
             @excerpt        被引用消息的 40 字以内摘要。纯文本原样保留，
                             富媒体折为语义占位。

<image/>     一张图片。像素不进入本信封，@desc 是该图内容的唯一表示。
             @kind     photo | sticker。photo 照片或截图，sticker 表情贴。
             @summary  QQ 自身的外显文案。
             @hash     该图内容的 sha256。meme_collection 与 look_at_image 以此定位。
             @desc     该图到达时生成的客观转录：画面内容与图上文字。
                       生成于该图到达时刻，不含此后的对话语境。
                       缺失 = 转录未成功（未配置视觉后端 / 调用失败 / 未下载成功）。

<face/>      QQ 原生黄豆表情。
             @face_id  QQ 内部表情 ID。与 <notice @likes> 中的 face:N、
                       出站 face 段的 data.id 同域。
             @name     该表情的释义。

<mface/>     商城 / 魔法表情。
             @summary  该表情的释义。

<voice/>     语音消息。无属性，内容不在信封内。

<video/>     视频消息。无属性，内容不在信封内。

<file/>      聊天中发送的文件。
             @name        文件名。
             @size_bytes  文件大小，单位字节。
             @file_id     平台侧文件凭证，供文件类工具回填。

<poke/>      消息内的拍一拍。
             @target_qq  被拍者的 QQ 用户号，等于 @bot_qq 即拍的是本账号。
                         缺失 = 该拍一拍无特定对象。

<dice/>      掷骰子结果。
             @value  点数，1 至 6。

<rps/>       猜拳结果。
             @value  1 石头 | 2 剪刀 | 3 布。

<markdown>   Markdown 富文本消息。正文为 markdown 源码，超 500 字截断（末尾 …）。
             空标签 = 内容不可获取。无属性。

<forward/>   合并转发的聊天记录包。包内消息不展开。
             @forward_id  该记录包的 ID。

<card/>      富文本分享卡片：链接分享、小程序、公众号文章、音乐、位置、名片推荐。
             @app      卡片的应用 ID。
             @summary  QQ 自身对该卡片的单行外显文案。
             @title    卡片自带标题。小程序卡片上通常是应用名。
             @desc     卡片自带描述。小程序卡片上通常是实际内容标题。
             @url      跳转链接。
             @format   json | xml | share。仅在卡片未能解析时出现，命名原生段格式。
                       仅带 @format 一个属性 = 内容未知。

<misc/>      运行时未识别的段。
             @segment_type  原生 OneBot 段类型。内容未知。


═══ 任务与时钟元素 ═══

<active-tasks>
  当前未收束的任务集合，位于 <timeline> 之后。

<task>  父 <active-tasks>
  一个未收束的任务。
  @task_id      任务 ID。call_tool 的 task_id 字段、以及以任务为目标的工具参数，都取此值。
  @state        pending | running。已收束（done / failed）的任务不在此集合。
  @description  创建该任务时写下的目标。

<related-tools>  父 <task>
  正文 = 逗号分隔的工具名，创建任务时声明的相关工具。它不构成调用约束。

<triggered-by/>  父 <task>
  @event_id  最初引发该任务的内部事件 ID。

<pending-tool-call-ids>  父 <task>
  正文 = 逗号分隔的调用 ID：为该任务分发、结果尚未回来的工具调用。

<progress-notes>  父 <task>
  该任务的进度笔记集合，仅保留最近数条。

<note>  父 <progress-notes>
  一条进度笔记，正文 = 某一拍 task 工具 note 分支写下的内容。
  @time  写下该笔记的时刻。

<current/>
  本次运行的时钟，位于信封末尾附近。
  @now   当前墙上时间。判定 <time @when> 的新旧以此为基准。

<validation-error>
  当同一拍的上一次输出被运行时拒绝时出现，是信封的最后一个元素，位于 <current/> 之后。
  正文 = 被拒的原因。该往返不进入任何对外可见记录。
