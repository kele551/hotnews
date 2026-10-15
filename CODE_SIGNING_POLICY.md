# 代码签名与产物验证政策（CODE SIGNING POLICY）

作者：**海风（kele551）** · 仓库：https://gitee.com/kele551/hotnews

适用范围：本项目在 Gitee / GitHub 发行版（Release）里分发的 Windows 产物
（`热点新闻.exe` 等），以及在线升级用的 `version.json` 与 `version.json.sig`。

---

## 一、发布产物来自本仓库源码构建

- 发布用的单文件 exe **一律由本仓库源码构建**，构建配置就在仓库里：
  `build_exe.spec`（PyInstaller 单文件，产出 `dist\热点新闻.exe`）。
- 构建命令与 README「打包」一节一致：

  ```bash
  pip install -r requirements.txt
  pip install pyinstaller
  python -m PyInstaller build_exe.spec --clean --noconfirm    # 产出 dist/热点新闻.exe
  ```

- **任何人都可以自己复现**：`.github/workflows/build.yml` 在 Windows runner 上装
  `requirements.txt` 的依赖与 PyInstaller，跑同一个 `build_exe.spec`，
  校验 exe 文件属性与 `version.json` 是否一致，打印每个产物的 SHA256 并上传 artifact。
  也就是说 —— **你不需要相信我们的 exe，可以自己构建一份来对照**。
- 仓库里**不放**任何预编译的 exe / zip 作为发布来源（`.gitignore` 已排除
  `*.exe`、`*.zip`、`dist/`、`build/`、`runtime/`）。
  你在 Release 里下到的那个 exe，就是上面这条命令的产物。

---

## 二、当前的签名状态（如实说明）

- **当前发布的 exe 没有 Authenticode 代码签名**（本项目没有购买商业代码签名证书）。
  因此首次运行时 Windows SmartScreen 可能提示「未知发布者」——
  这是**没有签名证书**的正常表现，不代表文件被改动过。
- 在没有签名的前提下，判断产物是否可信只有两条路：
  1. **自己按第一节的命令从源码构建**，与下载的包对照行为；
  2. **比对 SHA256**（见第四节）。
- 我们不会写「请放心运行」这种话 —— 请用上面两条自己核对。

---

## 三、将来若启用免费代码签名：由 CI 自动签名

如果将来接入**面向开源项目的免费代码签名**（例如 SignPath Foundation 这类
为开源项目提供免费签名的服务），执行方式现在就定下来，避免临时拍脑袋：

1. **签名只发生在 CI 里**。在 `.github/workflows/build.yml` 的「打单文件 exe」之后、
   「计算并打印 SHA256」之前插入签名步骤；签名后的产物再算 SHA256、再上传 artifact。
   （workflow 里已经留了注释标出这个位置。）
2. **私钥不进仓库、不进 PR**。签名证书与私钥只放在 CI 的加密 secrets 里，
   或者使用签名服务的托管签名（私钥根本不出对方环境）。
   仓库里任何文件、任何提交都不得包含私钥或证书口令。
3. **只签本仓库源码构建出来的产物**。CI 是唯一的签名入口；
   维护者本机手工构建的 exe **不签名**，也不会以已签名的形式发布。
4. **签署者信息**：签名主体为项目维护者 海风（kele551）；
   exe 文件属性里的署名为 `HaiFeng (kele551)`，
   版权行 `Copyright (C) 2026 HaiFeng (kele551)`，与现有产物保持一致。
5. **可核验**：签名一旦启用，Release 说明里会写明「已签名 / 未签名」，
   并给出 `Get-AuthenticodeSignature` 的核对命令与预期结果。
6. **签名不替代校验**：启用签名之后，第四节的 SHA256 与第五节的升级验签**照旧生效**，
   三者是叠加关系，不是替代关系。

---

## 四、怎么用 SHA256 验证你下到的产物

**Windows（PowerShell）：**

```powershell
Get-FileHash -Algorithm SHA256 .\热点新闻.exe | Format-List
# 输出里的 Hash 就是本地算出来的 SHA256（十六进制，大小写不敏感）
```

**比对对象（按可信度排序）：**

1. **自己构建的那一份**：按第一节的命令从源码构建，与下载的包比对。
   注意两份构建如果 PyInstaller / Python 版本不同，字节会不同、SHA256 也就不同；
   这时以「自己构建的包能正常跑、行为一致」为准，并欢迎把差异反馈给我们。
2. **远端 `version.json` 里的 `launcher.sha256`**：这就是升级流程用的期望值。
   更新源：`https://raw.giteeusercontent.com/kele551/hotnews/raw/master/version.json`
   （字段为 64 位十六进制）。程序自己升级时**强制校验**它，不符就拒绝替换。
3. **CI 打印的 SHA256**：`.github/workflows/build.yml` 每次运行都会把产物的 SHA256
   写进日志，并写进 artifact 里的 `SHA256SUMS.txt`。

> **注意 SHA256 的边界**：如果摘要和文件放在同一个地方（同一个 Release、同一个仓库），
> 它只能防「下载损坏」，防不了「账号被盗之后把文件和摘要一起换掉」。
> 这正是下一节存在的原因。

---

## 五、本项目的升级通道已有 Ed25519 签名校验

在线升级不是只靠 SHA256，而是**两层校验**：

| 层 | 校验什么 | 实现位置 | 失败时的行为 |
|---|---|---|---|
| 第一层 | `version.json` 的 **Ed25519 签名** | 客户端内置公钥 `UPDATE_PUBKEY`（`updater.py`），逐字节校验远端 `version.json` 与同目录的 `version.json.sig` | **拒绝采用**这份版本信息（fail-closed），当作「没有新版本」 |
| 第二层 | 升级包（exe）的 **SHA256 + 文件尺寸** | `updater.py`：下载到 `.part` 临时文件后校验一次，替换 exe 之前**再校验一次** | **拒绝替换**，保留旧版，程序继续可用 |

- 签名算法是仓库内的纯 Python Ed25519 实现 `_ed25519.py`（零第三方依赖，只用 `hashlib`），
  正确性由 RFC 8032 官方测试向量验证。
- **私钥只在维护者的离线环境里**：不进仓库、不进 CI、不进任何提交。
  仓库里只有客户端验签用的公钥（`updater.py` 的 `UPDATE_PUBKEY`）与签名文件 `version.json.sig`。
- 这样即便更新源账号被盗、有人推了恶意 `version.json` 加恶意 exe，
  **没有私钥就签不出合法签名**，客户端会直接拒绝升级。
- 升级过程还做了原子化处理：先下到 `.part`、校验通过再改名，
  替换前再核对一次尺寸与 SHA256 —— 曾经因为重复触发「检查更新」把已下好的包截断，
  把一个 14MB 的程序覆盖成 1.2MB 的残缺文件，程序当场报废，所以这几道必须都在。
- 相关实现：`updater.py`（`fetch_remote` / `_verify_sig` / `download_and_prepare` /
  `trigger_replace`）、`_ed25519.py`。**发布端的签名脚本在维护者工作区，不随仓库分发。**

---

## 六、修订

本政策随发布流程的变化更新；有实质变更会在 Release 说明里点出来。
