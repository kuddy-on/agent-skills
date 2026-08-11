# Agent Skills

[![Test](https://github.com/kuddy-on/agent-skills/actions/workflows/test.yml/badge.svg)](https://github.com/kuddy-on/agent-skills/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一组可复用的 Agent Skills，用于审查、合并和发布托管在 Gitea 上的仓库。

[English](README.md)

## 可用技能

| Skill | 用途 |
| --- | --- |
| `gitea-review` | 审查或复审 Gitea Pull Request，并提交正式 Review。 |
| `gitea-merge` | 校验普通 Pull Request，并使用 rebase、squash 或 merge commit 合并。 |
| `gitea-release` | 合并 Release Please Pull Request，并验证 Workflow、Tag 和 Gitea Release。 |

三个 Skill 均可独立安装和使用。`gitea-release` 不依赖 `gitea-merge`，并且只处理已经
存在的 Release Please Pull Request。

`gitea-review` 会先根据元数据判断 PR 规模，再交给一个不继承当前对话且可复用的 worker。
快速和聚焦复审使用 medium reasoning，中型 PR 使用 high，大型 PR 使用 xhigh；父 Agent
不会读取 patch。

## 环境要求

- 支持 [Agent Skills](https://agentskills.io/) 格式的 Agent 运行环境
- Git 和 Python 3.11 或更高版本
- 已为目标 Gitea 实例配置的 [Tea](https://gitea.com/gitea/tea)
- 使用 `skills` CLI 安装时需要 Node.js 和 npm

凭据只保存在 Tea 的本地凭据存储中。Skill 不要求将 Token、密码或私钥提交到本仓库。

## 安装

为 Codex 全局安装全部 Skill：

```bash
npx skills add kuddy-on/agent-skills --skill '*' --global --agent codex --yes
```

安装单个 Skill：

```bash
npx skills add kuddy-on/agent-skills --skill gitea-merge --global --agent codex --yes
```

## 更新

```bash
npx skills update --global --yes
```

更新命令只刷新已经安装的 Skill。仓库新增或重命名 Skill 后，需要重新运行安装命令。

## 沙盒权限

三个 Skill 都需要通过网络访问 Gitea。在受管理的沙盒中，Agent 应在第一次执行脚本时
直接申请网络提权，不应先在受限沙盒中运行 Tea。脚本可以直接执行，因此长期授权可以
限制到具体脚本，而不是放开通用 Python 解释器。

## 开发

先安装开发工具（ShellCheck 使用操作系统的软件包管理器安装），再运行格式与静态检查：

```bash
python -m pip install --requirement requirements-dev.txt
go install mvdan.cc/sh/v3/cmd/shfmt@v3.13.1
scripts/check.sh
```

运行单元测试：

```bash
python -m unittest discover -s tests -v
```

运行隔离集成测试：

```bash
tests/integration/run.sh
```

集成测试会创建临时本地 Gitea 容器和仓库，执行一次真实的 Pull Request 合并，并在结束
时销毁全部临时资源。它不会使用已配置的生产 Gitea 账号。

## 仓库结构

```text
agent-skills/
├── scripts/
├── skills/
│   ├── gitea-merge/
│   ├── gitea-release/
│   └── gitea-review/
├── tests/
├── AGENTS.md
├── CONTRIBUTING.md
├── LICENSE
├── README.md
└── README.zh-CN.md
```

## 贡献与支持

提交 Pull Request 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，使用问题请参考
[SUPPORT.md](SUPPORT.md)。安全漏洞请按照 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

本项目使用 [MIT License](LICENSE)。
