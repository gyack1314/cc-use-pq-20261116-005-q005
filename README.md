# 气候指标批次采集台账

本项目供研究团队离线接收不同机构交付的气候指标摘录。系统核验来源和指标契约，保留每次批次修订，记录质量证据，并发布不可变、可校验的快照。演示数据均为本地合成数据，不代表真实气候观测，也不在运行时访问外部网站。

使用 Python 3.11 或更新版本即可运行，无第三方依赖。以下命令均从项目根目录执行。

## 快速演示

运行 python -m climateledger demo --workspace fixtures，完成本地演示：创建 SQLite 数据库、注册合成指标与来源、摄入两行数据、发布快照并校验导出。演示随后执行区域演练（region_drill）：同一月份全国、两个省级/沿海区域和旧文件四类记录并存；仅更正一个省的值并展示 compare 只把变化归到该区域；验证旧文件落在 unscoped；验证未授权区域被拒绝。再次执行会重用相同批次和发布版本。

单独使用时，把 --db path/to/ledger.sqlite3 放在子命令前。依次执行 register-indicator contract.json、register-source contract.json；需要单位换算时执行 register-unit rule.json。用 ingest batch.manifest.json 摄入数据，batch ID 查看记录；待审批次用 approve ID REVISION 审核，然后用 publish ID REVISION 发布。

export VERSION --output bundle.json 导出指定快照，verify bundle.json 检查摘要，series INDICATOR --version VERSION [--region REGION] 查询发布值。coverage、discrepancies、compare、replay 和 audit 分别用于查看覆盖、来源分歧、版本变化、原始文件重放及审计链；这些查询都接受 --region，并且不会用其他区域的值填补缺失。compare 不按区域过滤，因为跨区域的变化归属本身就是要展示的信息。

## 输入与状态

指标契约包含 key、title、canonical_unit 和 cadence（月份或年份）；可附带上下限、精度和负值许可。来源契约包含标识、标题、授权说明、允许提交的指标列表，以及可选的 allowed_regions。省略 allowed_regions 表示不限制区域；显式列出则只授权这些区域，提交未授权区域会形成 region_scope_violation 拒收证据。相同标识下契约不可静默改写。单位规则采用“原值乘以 factor 加 offset”换算为规范单位。

## 区域口径

区域是显式的一等标识，不与指标或期间合并。采集文件有三种确定的口径：清单顶层 region 适用于全部行；列映射包含 region 时逐行读取（此时不能再给顶层 region）；两者都没有时，全部行进入保留区域 unscoped。unscoped 是旧文件的唯一解释，系统绝不把它推断成任何省或全国区域。批次同时记录 region_origin（manifest、rows 或 legacy_default）和解释文本，并随发布快照保留，因此旧文件的重放结果是可复现的。空白或非法的区域单元格按行拒收，不会静默落到某个缺省区域；逐行区域的清单若缺少 region 列，则与缺少其他必需列一样整批拒收。

批次清单指定来源、外部批次编号、同目录的 CSV 或 JSON 数据文件、文件 SHA-256、行数及列映射：indicator、period、value、unit 为必需，region 可选。文件仅从本地读取。非法行形成拒收证据；重复、冲突、缺期和已发布值的变更都在同一区域内判断：同月不同区域是两条独立观测，一个区域缺期不会被另一区域的值填补。相同字节重复提交幂等，内容变化形成修订。清单中的行数和摘要可检测不完整或被修改的输入。

发布版本包含上个版本的摘要、来源文件摘要、批次修订、当前测量集合、区域清单及导出摘要。每条测量都带区域，发布清单记录本批次的区域出处；不同来源、不同区域对同一指标和期间的观测保持独立。区域查询是严格的：请求 province_X 时只返回该区域的记录，不会回退到全国值。旧版本始终可以独立导出和验证；升级前生成的无区域快照仍按原摘要校验，其数据以 unscoped 解释。

## 验证命令

python -m unittest discover -s tests -v

python -m compileall -q climateledger

python -m climateledger demo --workspace fixtures
