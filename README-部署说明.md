# InvenTree 群晖部署说明（DS920+ / amd64 / DSM 7.3.2）

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

本目录 `plugins/weiti_mdm/` 是物料主数据插件（类别/选项编号 + IPN + BOM导入），
`scripts/assign_ipns.py` 是存量零件批量补码脚本。

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
槽位码: 参数没填全时发槽位码，已填槽位出码、未填槽位用 x 占位
        （如 102-03xx103xx = 类别已定/封装未定/10K/精度功率未定）；
        参数补齐后自动升级为正式码
复核码: ! 前缀仅用于「导入建件时规格反解失败、槽位留了默认值」的零件，
        提示人工复核（如 !102-03xx103xx）；补齐参数后 ! 随升级消失
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

### BOM 一键导入（去重 + 增量建零件）

插件页面地址：`http://192.168.1.188:1337/plugin/weiti_mdm/bom-import/`
（需以员工 staff 身份登录）。

流程：
1. **上传**：选 BOM 文件（xlsx/xls/csv）+ 填父零件（ID/IPN/名称）；
2. **映射列**：页面自动检测列名并猜默认映射，确认「组件名称/数量/规格/类别/位号」对应列；
3. **预览**：点「预览」跑 dry-run（不落库），看将新建/复用/挂行/失败；
4. **确认导入**：核对无误后落库。

去重键 = **名称 + IPN 特征段**：同名且规格特征相同 → 复用；同名但规格不同 → 另建。
缺料零件自动建档（类别走归类逻辑 → 自动 IPN），规格写入描述并反解参数。

**缺失类别确认**：预览页会列出 Excel 中匹配不到系统的类别，逐个可选
「新建到某大类下（自动编号）/ 映射到已有类别 / 不归类」；
确认导入时先建类别、再建零件、最后挂 BOM。

> 依赖插件文件：`bom_import.py` 与 `templates/weiti_mdm/*.html`，
> 与 `__init__.py` 一起放在 `plugins/weiti_mdm/` 下，改动后重启容器。

### 存量零件批量补码

```bash
# 先演练看结果，不落库：
sudo docker exec -it inventree-server \
    python /home/inventree/data/scripts/assign_ipns.py --dry-run

# 确认无误后正式执行：
sudo docker exec -it inventree-server \
    python /home/inventree/data/scripts/assign_ipns.py
```

### 注意

- 参数没填全的零件不会编码（脚本会列出 `[跳过]` 清单）；
- 类别名不带码前缀（如「结构类」而非「2-结构类」）时，其下小类无法自动编号；
- 已有合规 IPN 的零件不会被改写，可反复执行。
