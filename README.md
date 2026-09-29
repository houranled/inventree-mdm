# InvenTree 说明+微体物料业务插件weiti_mdm（DS920+ / amd64 / DSM 7.3.2）

访问地址：`http://192.168.1.188:1337`（HTTP 直连，不启用 TLS，避开 DSM 的 80/443）

本目录三件套：
- `docker-compose.yml`：5 个容器（db / cache / server / worker / proxy）
- `.env`：所有可调参数（端口、IP、数据库密码、持久化路径）
- `Caddyfile`：反代配置（官方原版）

## 一、放置文件

1. File Station 打开 `docker` 共享文件夹，新建目录 `inventree`，其内再建 `data`。
2. 把本目录的 `docker-compose.yml`、`.env`、`Caddyfile` 三个文件放到 `docker/inventree/`。
   （持久化路径 `/volume1/docker/inventree/data` 已写在 `.env` 里）

## 二、改密码（必做）

编辑 `.env`，把 `INVENTREE_DB_PASSWORD` 改成你自己的强密码。

## 三、初始化（SSH 执行）

控制面板 → 终端机和 SNMP → 启用 SSH，然后 SSH 登录群晖：

```bash
cd /volume1/docker/inventree
sudo docker compose run --rm inventree-server invoke update      # 建库 + 迁移
sudo docker compose run --rm inventree-server invoke superuser   # 建管理员账号（按提示输入用户名/邮箱/密码）
```

## 四、启动

```bash
sudo docker compose up -d
```

或在「Container Manager → 项目 → 新增」中选择 `docker/inventree` 目录用现成 compose 启动；
但第三步的一次性 `invoke` 命令建议仍用 SSH 跑。

## 五、访问

浏览器打开 `http://192.168.1.188:1337`，用第三步创建的管理员账号登录。

## 常见问题

- **端口冲突**：若 1337 被占用，改两处并保持一致：`.env` 的 `INVENTREE_SITE_URL` 端口 + `docker-compose.yml` 中 `"1337:1337"`。
- **首次 `invoke update` 较慢**：会拉镜像 + 初始化，数分钟属正常。
- **资源**：DS920+（J4125 / 4GB）可正常运行；加内存更稳。
- **升级**：`cd /volume1/docker/inventree && sudo docker compose pull && sudo docker compose up -d`。
- **想走 HTTPS + 域名**：改用群晖「控制面板 → 登录门户 → 反向代理」指向本机 1337，并在自定义标头加 WebSocket；同时把 `.env` 的 `INVENTREE_SITE_URL` 改成对应 https 域名。

## 六、物料主数据插件 weiti_mdm（自定义）

本目录 `plugins/weiti_mdm/` 是物料主数据插件（类别/选项编号 + IPN
+ BOM导入/导出 + 供应商导入），
`scripts/assign_ipns.py` 是存量零件批量补码脚本，
`scripts/backfill_params.py` 是存量零件补建类别参数行脚本。

### 部署

```bash
# 1. 把插件和脚本放进数据卷（File Station 或 SSH 均可）
#    → /volume1/docker/inventree/data/plugins/weiti_mdm/__init__.py
#    → /volume1/docker/inventree/data/scripts/assign_ipns.py

# 2. 重启使插件加载
cd /volume1/docker/inventree && sudo docker compose restart

# 3. 浏览器：管理员中心 → 插件 → 启用「微体物料主数据」
```

### 编码规则（文档仅作格式参考，码值自动分配）

```
IPN = {小类码}-{规格段}   例: 102-030110301（无流水号，同规格同码）

小类码: 大类(1~8)手动定；小类创建时自动取该号段下一个可用码，
        并自动改写名称（在「1-电子组件」下建「电阻」→「102-电阻」）
选项码: 参数模板选项自动编号——只写"金属膜/碳膜"，
        保存后自动变"01-金属膜/02-碳膜"；已带码的保留原始位数
        （精度"0-±1%"是1位码，类别"03-金属膜"是2位码——位宽由编码表定）
规格段: 按类别参数模板顺序，取每个参数值的前缀码拼接
        "03-金属膜"→03, "01-SMD0603"→01, "10K"→103(有效数字+10的幂),
        "0-±1%"→0, "1-1/16w"→1  →  102-030110301
槽位码: 参数没填全时发槽位码，一律以 ! 开头（待完善/复核标记），
        已填槽位出码、未填槽位用 ? 占位
        （占位宽度=该字段位宽：阻值3位→???、精度1位→?、封装2位→??）
        如 !102-03??103? = 类别已定/封装未定(2位)/10K/精度未定(1位)；
        无论自动还是手工创建，只要含未设置的参数就带 !；
        参数补齐后自动升级为正式码、! 消失
无规格码: 类别没绑参数模板（本来就没规格可填）时发正式流水码 805-S0001
        （S=Spec-less，非临时态不升级）；旧 !805-T0001 占位码重存自动迁移
```

**日常操作不需要查编码文档**：人只写语义名（类别名/选项名），所有码由系统分配。

### Excel 导入自动兼容（旧类别名/中文单位）

零件导入向导里把 Excel「类别」列映射到 **keywords（关键字）** 字段，
插件在落盘前自动归类（keywords 内容保留，还顺带提升中文搜索命中）：

- 旧名匹配顺序：`alias.txt` 别名表 → 类别全名 → 类别去码名
  （`紧固/定位`→别名表命中 201；`螺丝螺母`→去码名直接命中 `201-螺丝螺母`）
- 别名表：`plugins/weiti_mdm/alias.txt`，每行 `旧名=类别码`，改后重启容器生效
- 中文单位自动翻译：`个/片/只/套...` → `pcs`，`米`→`m` 等（表在插件 `UNIT_ALIAS`）

### 从描述反解参数（插件自动，无需脚本）

零件导入时把「规格型号」列映射到原生 **description** 字段即可——
插件在零件保存后（含导入）**自动**按其类别的参数模板从描述里匹配参数值并写入，
写入参数会触发 IPN 从临时码升级为正式码。全自动，不用手动跑命令。

匹配规则：模板有选项 → 在描述里找选项文本（`01-SMD0603`/`SMD0603`/`0603` 都能命中）；
阻值类模板 → 正则抽 `10K/4.7K` 等；匹配不到就跳过，不乱填、不覆盖已有值。

开关：插件 `ENABLE_DESC_EXTRACT`（默认开）。

> 存量老零件（插件部署前已导入的）不会自动回溯，需重新保存一次触发，
> 或用维护脚本 `scripts/extract_params.py` 批量处理（支持 `--dry-run`/`--category`/`--overwrite`）。

### BOM 一键导入（多工作表 + 去重 + 增量建零件）

插件页面地址：`http://192.168.1.188:1337/plugin/weiti_mdm/bom-import/`
（需以员工 staff 身份登录）。入口：零件详情页「BOM导入」按钮、
`Ctrl+K` 搜索「BOM导入」、或首页仪表盘「物料工具」卡片。
（不走左侧导航——navigation 特性只支持 SPA 内部路由，指插件页会 404。）

#### 多工作表语义

一个 Excel 可含多个 tab，**每个 tab 是一个装配体的 BOM**，表头在前 3 行内自动识别（首行是标题/说明也能跳过）。父零件框决定层级：

- **留空**：每个 tab 按表名（去掉 `BOM` 及之后内容）各建/复用一个**成品类父零件**，行挂到各自下面。
  例：`间隙传感器BOM清单` → 零件「间隙传感器」
- **填写**（ID/IPN/名称，详情页按钮带入）：每个 tab 按表名各建/复用一个**子装配件**，
  先作为 `x1` 的 BOM 行挂到该父零件下，再把表内各行挂到子装配件下。
  例：父零件=间隙采集系统 → 其 BOM 下出现 间隙传感器/轮毂配电柜线材/… 各 x1。

父零件在**确认导入时才真正创建**（上传和预览只探测不落库；预览时临时建完即回滚）。
同名循环挂载（子件名 = 父件名 或会成环）会被拦截并在报告中标记失败。

#### 流程

1. **上传**：选 BOM 文件（xlsx/xls/csv）；父零件可留空；
2. **映射列**：每个 tab 一张卡片独立配置（名称必填，数量/规格多选/类别可选），
   卡片左上角「导入此表」勾选框默认勾选，取消则该表整表跳过；
   每卡片只预览前 3 行；列名自动猜测（类别列优先「类别」、其次「类型」）；
   **位号列不配置**——表头含 `位号/ref/designator` 自动写入 BOM 位号；
3. **预览**：dry-run 不落库，按工作表分组展示：每组的父零件、新建/复用/BOM行/失败小计、
   明细行（行号/名称/动作/类别/说明）；指定父零件时组内第一行是 `link` 挂载行；
4. **确认导入**：核对无误后落库。

去重键 = **名称 + IPN 特征段**：同名且规格特征相同 → 复用；同名但规格不同 → 另建。
缺料零件自动建档（类别走归类逻辑 → 自动 IPN），规格写入描述并反解参数；
新建零件自动**复制类别参数模板**（等价于网页建件时勾"复制类别参数"）。

**图片**：单元格内嵌图片（WPS `DISPIMG` 及常规浮动图）随零件导入 `part.image`，
逐表独立映射行号互不串；零件已有图则跳过。

**缺失类别确认**：预览页列出匹配不到的类别文本，逐个可选
「新建到某大类下（自动编号）/ 映射到已有类别（大类分组标题不可选）/ 不归类」；
确认导入时先建类别、再建零件、最后挂 BOM。

#### BOM 导出（对称能力）

零件详情页按钮：**导出BOM**（本件 1 个 tab）、**导出BOM树**（递归所有下级装配体，
每个一个 tab，含嵌入图片）。另有手工多选导出：
`/plugin/weiti_mdm/bom-export-multi/?pks=1,2,3`。

> 依赖插件文件：`bom_import.py`、`bom_export.py` 与 `templates/weiti_mdm/*.html`，
> 与 `__init__.py` 一起放在 `plugins/weiti_mdm/` 下，改动后重启容器。

### 供应商信息导入（零件 ↔ 供应商/制造商关联）

插件页面地址：`http://192.168.1.188:1337/plugin/weiti_mdm/supplier-import/`
（`Ctrl+K` 搜「供应商导入」或首页「物料工具」卡片直达）。用于把供应商/报价文件里的
采购信息挂到**已有零件**上——典型用法是先 BOM 导入建零件，再导供应商清单补采购数据。
映射页同样支持逐表勾选，取消勾选的表整表跳过。

#### 落库对象

| Excel 列（可映射） | 写入模型 | 去重键 |
|---|---|---|
| 零件标识（IPN 或名称，必填） | 匹配 `Part`（先 IPN 精确，再名称） | — |
| 供应商（必填） | `Company`（自动补 `is_supplier`） | 名称 |
| 制造商/品牌 + 制造商型号 | `Company`(`is_manufacturer`) + `ManufacturerPart` | (零件, 制造商, MPN) |
| 供应商料号 SKU | `SupplierPart.SKU`（留空用零件 IPN） | (零件, 供应商, SKU) |
| 单价 + 起订量 | `SupplierPriceBreak` | (供应商件, 数量)，同量更新价格 |
| 备注 | `SupplierPart.note` | — |

#### 流程

1. **上传**：供应商/报价文件（xlsx/xls/csv），多 tab 均会解析；
2. **映射列**：每 tab 独立配置，零件标识和供应商必填；页面顶部统一选币种
   （默认 CNY）；
3. **预览**：事务回滚不落库，按工作表分组报告新建/复用/价格/失败
   ——零件找不到、名称多匹配、单价无法解析都会标红；
4. **确认导入**：每表一个事务，失败整表回滚不中断后续表。

复用已有 `SupplierPart` 时会顺带补 `manufacturer_part` 链接和空的 `note`；
复用 `Company` 时按需补 `is_supplier`/`is_manufacturer` 标记。

> 依赖插件文件：`supplier_import.py` 与 `templates/sup_*.html`。

### 存量零件批量补码

```bash
# 先演练看结果，不落库：
sudo docker exec -it inventree-server \
    python /home/inventree/data/scripts/assign_ipns.py --dry-run

# 确认无误后正式执行：
sudo docker exec -it inventree-server \
    python /home/inventree/data/scripts/assign_ipns.py
```

### 存量零件补建参数行

插件 ORM 直建的零件不会自动复制类别参数模板（API 建件才有），
导致参数页无字段可填。补跑（幂等，已齐的自动跳过）：

```bash
sudo docker exec -it inventree-server \
    python /home/inventree/data/scripts/backfill_params.py --dry-run

sudo docker exec -it inventree-server \
    python /home/inventree/data/scripts/backfill_params.py
```

### 注意

- 参数没填全的零件不会编码（脚本会列出 `[跳过]` 清单）；
- 类别名不带码前缀（如「结构类」而非「2-结构类」）时，其下小类无法自动编号；
- 已有合规 IPN 的零件不会被改写，可反复执行。

### 订单联动闭环（SO→BO→PO）+ 齐套通知 + 排单

标准 MRP 方向：销售订单是需求源头，向下传导。

```
SO 建行项目(post_save)   → 逐行查缺口(quantity − shipped − allocated
   ├─ 可自制 → 自动生成 PENDING 生产单（sales_order 字段回链来源）   − 可用 − 在产 − 在途)
   └─ 可外购 → 按供应商分组自动生成 PENDING 采购单
BO 建立/行保存           → BOM 行缺料：外购件挂 PO 行；
                            装配件由原生 Auto Create Builds 建子 BO
                            （该插件未启用时本插件兜底，挂 parent 父子关系）
SO/BO 取消               → 自动取消其下游仍为 PENDING 的生成单
PO 到货 / 库存变动        → 未齐套订单自动重查
```

触发时机说明：**行项目保存（含新建）即触发**，不需要等订单下达；
下游单全部生成为 `PENDING` 待审态，需人工下达。缺口按净额计算
（扣在产、在途），行项目改数量后自动补差，重复触发不重复建单。
信号经 `transaction.on_commit + offload_task` 投递给后台 worker 执行。

- **自制/外购判定**：只能走一种的直接走；两者皆可时比交期——
  `part.metadata['lead_time_days']`（自制周期）vs
  `SupplierPart.metadata['lead_time_days']`（取最小供应商交期），
  没数据用插件设置的默认天数，打平按「自制/外购兜底」设置。
- **齐套口径**：`part.available_stock ≥ 尚未分配的需求`（"库存够发"，
  不要求先做分配动作）。标记写 `order.metadata`：
  `weiti_kitted` / `weiti_shortages`（缺料明细）。
- **齐套通知**：只在 不齐套→齐套 跳变时发——站内通知铃 +
  企微群机器人 webhook（markdown 消息带订单链接）。
  收件人 = 插件设置里的生产组/销售组（Django Group）全员 +
  订单 `responsible` 负责人。
- **排单**：`priority = 交期得分×权重 + 金额得分 + 上游传导`。
  生产单写原生 `priority` 字段，销售/采购单写 `metadata['weiti_priority']`。
  看板页 `/plugin/weiti_mdm/schedule/`（Ctrl+K 搜「排单看板」，
  或仪表盘「急单提醒」卡片），页面有「重算优先级」按钮；
  另有每日定时任务全量兜底。
- **订单关联溯源**：生成的 PO 在描述里写明来源（"自动生成：为
  BO-0007 采购缺料"），`link` 字段可点击跳回来源订单，行项目
  `notes` 记来源单号；详情页另有面板：BO/SO 页显示「关联采购单」
  列表，PO 页显示「来源订单」。
- **零件级"按BOM采购"**：有 BOM 的零件详情页标题栏出现
  「按BOM采购」按钮 → `/plugin/weiti_mdm/part-po/<pk>/` 预览页。
  输入备货数量后**逐层下钻到最底层可采购件**（有下层 BOM 的子件
  视为制造继续下钻），净缺口按供应商分组生成 PENDING 采购单；
  无供应商/不可采购/库存已覆盖的项在"跳过项"中列明原因。

**插件设置**（管理员中心 → 插件 → WeiTiMDM → 设置）：

| 键 | 说明 | 默认 |
|---|---|---|
| `OF_ENABLE` | 联动总开关 | 开 |
| `OF_WECOM_WEBHOOK` | 企微机器人完整 URL（空=只发站内） | 空 |
| `OF_GROUP_PROD` / `OF_GROUP_SALES` | 生产/销售通知组名 | 生产 / 销售 |
| `OF_BUILD_DAYS` / `OF_PURCHASE_DAYS` | 默认生产/采购周期 | 7 / 14 天 |
| `OF_MAKE_OR_BUY` | 交期打平兜底 | 外购 |
| `OF_PRIO_*_W` | 优先级三项权重 | 1.0 / 1.0 / 0.5 |

> 依赖插件文件：`orderflow.py`、`weiti_notify.py`、
> `templates/schedule_board.html`、`templates/part_po.html`。
> 定时任务需系统设置开启 `ENABLE_PLUGINS_SCHEDULE`。

## 七、用户组权限建议（分组角色）

管理员中心 → 用户 → 用户组 → 「分组角色」，按 视图/更改/添加/删除 四列勾选。
✅=建议勾选，➖=按需勾选，空=不勾。

分工原则：

- **零件/零件类别/物料清单（BOM）**＝主数据，归工程研发维护，其他角色默认只读；
- **库存项/库存地点**＝库管管"有多少、在哪"，给增改、删只给主管；
- **采购订单的"更改"给库管**：对采购单收货入库需要此权限，但建单归采购；
- **销售订单的"更改"必须给销售**：给订单分配库存发货在订单页操作；
- **删除权限一律收紧**：零件/BOM/订单的删除只给各组主管或管理员；
- 用 Authentik SSO 时，把 IdP 组名映射到上述本地组即可自动分组（前提是 SSO 登录已跑通）。
