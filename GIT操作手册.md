# Git 生产级操作手册

> 本手册覆盖日常开发中所有 Git 操作场景。每个操作都有**示例命令**，直接复制可用。
> 操作目录：`/Users/yangkun/Desktop/Projects/ags_all`
> 远程仓库：`https://github.com/ykwolf1/AgentForge`

---

## 一、首次配置（只需做一次）

```bash
# 设置你的身份（提交记录里会显示这个名字和邮箱）
git config --global user.name "yk"
git config --global user.email "yk201910250@outlook.com"

# 验证配置
git config --global --list
```

---

## 二、日常推送流程（改了代码 → 推到 GitHub）

这是**最常用的操作**。每次改完代码后执行：

```bash
# 第 1 步：看改了什么（不会提交，只是看）
git status

# 输出示例：
# modified:   agentforge/agents/agent.py      ← 改了这个文件
# modified:   README.md                       ← 改了这个文件
# Untracked files:                            ← 新增了文件
#   agentforge/new_module.py

# 第 2 步：把改动加入暂存区
git add -A                          # -A 表示全部（改的+删的+新增的）

# 或者只加特定文件（更精确）
git add agentforge/agents/agent.py README.md

# 第 3 步：提交（写清楚改了什么）
git commit -m "修复 agent loop 错误处理 + 更新 README"

# 第 4 步：推送到 GitHub
git push origin main
```

**完整示例：你修了一个 bug**

```bash
cd /Users/yangkun/Desktop/Projects/ags_all

# 改完代码后
git status
# modified:   agentforge/agents/agent.py

git add agentforge/agents/agent.py
git commit -m "修复：LLM 断流时半残消息没回滚导致下轮 400"
git push origin main

# 完成！GitHub 上已经更新了
```

---

## 三、拉取最新内容（GitHub 上有更新 → 同步到本地）

**场景**：在另一台电脑上改了代码推到了 GitHub，或者别人帮你改了代码，你要同步到本地。

```bash
# 拉取远程最新代码（自动合并到当前分支）
git pull origin main

# 如果有冲突（同一个文件你也在本地改了），会提示：
# CONFLICT (content): Merge conflict in agentforge/agents/agent.py
# 这时打开文件，找到 <<<<<<< 标记，手动选择保留哪段，然后：
git add agentforge/agents/agent.py
git commit -m "合并远程更新"
git push origin main
```

**完整示例：另一台电脑改了代码，你在这台同步**

```bash
cd /Users/yangkun/Desktop/Projects/ags_all

# 拉取最新
git pull origin main
# Updating 18cfcdf..a3b4c5d
# Fast-forward
#  agentforge/agents/agent.py | 5 +++--
#  1 file changed, 3 insertions(+), 2 deletions(-)

# 现在你的本地代码和 GitHub 一致了
```

---

## 四、分支管理（开发新功能不影响 main）

**为什么用分支**：main 是稳定的。开发新功能时建一个分支，改完确认没问题再合并回 main。如果改崩了，直接删掉分支，main 不受影响。

### 4.1 创建分支 + 开发 + 合并

```bash
# 第 1 步：基于 main 创建新分支
git checkout -b feature/add-ocr        # checkout -b = 创建并切换

# 现在你在 feature/add-ocr 分支上（不在 main 上）
# 改代码、测试、提交……
git add -A
git commit -m "新增：OCR 工具支持扫描版 PDF"

# 第 2 步：开发完了，切回 main
git checkout main

# 第 3 步：把分支合并到 main
git merge feature/add-ocr

# 第 4 步：推送到 GitHub
git push origin main

# 第 5 步：删除已合并的分支（保持整洁）
git branch -d feature/add-ocr
```

**完整示例：开发 OCR 功能**

```bash
cd /Users/yangkun/Desktop/Projects/ags_all

# 建分支
git checkout -b feature/ocr-support
# Switched to a new branch 'feature/ocr-support'

# 写代码……（新建了 agentforge/tools/misc/ocr.py）
# 测试……（跑了 pytest）

# 提交
git add -A
git commit -m "新增 OCR 工具：支持扫描版 PDF 文字提取"

# 还没推到 GitHub，先确认没问题
# 如果测试没过，继续在这个分支改

# 测试通过了，合并到 main
git checkout main                    # 切回 main
git merge feature/ocr-support        # 合并
git push origin main                 # 推送

# 清理
git branch -d feature/ocr-support    # 删掉分支
```

### 4.2 推送分支到 GitHub（不合并 main，让别人看）

```bash
# 把分支也推到 GitHub
git push origin feature/ocr-support

# 别人可以拉你的分支看：
# git fetch origin
# git checkout feature/ocr-support
```

### 4.3 放弃一个分支（改崩了，不要了）

```bash
# 如果分支还没合并到 main，直接删：
git checkout main                    # 先切回 main
git branch -D feature/ocr-support    # -D 强制删除（-d 只能删已合并的）

# 如果已经改乱了 main，想恢复到上次提交的状态：
git checkout -- .                    # 放弃所有未提交的改动
git reset --hard HEAD                # 同上（更彻底）
```

---

## 五、查看历史和差异

```bash
# 查看提交历史（谁在什么时候改了什么）
git log --oneline -10                # 最近 10 条提交

# 输出示例：
# 18cfcdf 清理无关文件 + README 更新
# 9680dd7 AgentForge: 生产级多 Agent 系统

# 查看某次提交改了什么
git show 18cfcdf                     # 看这个提交的详细改动

# 查看当前未提交的改动
git diff                             # 看所有未暂存的改动
git diff agentforge/agents/agent.py  # 看某个文件的改动

# 查看本地和远程的差异
git fetch origin
git log main..origin/main            # 远程比本地多了哪些提交
```

---

## 六、撤销操作

### 6.1 撤销未提交的改动

```bash
# 改了文件但还没 git add，想恢复原样
git checkout -- agentforge/agents/agent.py

# 或者恢复所有文件
git checkout -- .
```

### 6.2 撤销已 add 但未 commit 的

```bash
git reset HEAD agentforge/agents/agent.py   # 移出暂存区（文件改动还在）
```

### 6.3 撤销已 commit 但未 push 的

```bash
# 撤销最近 1 次提交（代码改动保留，只是撤销提交）
git reset --soft HEAD~1

# 撤销最近 1 次提交（代码改动也丢弃）
git reset --hard HEAD~1
```

### 6.4 撤销已 push 的（已推到 GitHub）

```bash
# 不要用 git reset（会让远程历史混乱）
# 用 git revert（创建一个"反向提交"来撤销）
git revert HEAD                       # 撤销最近一次提交
git push origin main                  # 推送这个撤销

# 别人 pull 就能看到"改回去了"，历史是干净的
```

---

## 七、常见问题处理

### Q1：push 时提示 "Updates were rejected"

```bash
# 说明远程有你本地没有的更新，先 pull 再 push
git pull origin main
git push origin main
```

### Q2：pull 时有冲突

```bash
git pull origin main
# CONFLICT (content): Merge conflict in README.md

# 打开 README.md，找到冲突标记：
# <<<<<<< HEAD
# 这是你本地的内容
# =======
# 这是远程的内容
# >>>>>>> origin/main

# 手动编辑：删掉标记，保留你要的内容，然后：
git add README.md
git commit -m "合并冲突解决"
git push origin main
```

### Q3：不小心提交了 .env / API key

```bash
# 如果还没 push：
git reset HEAD~1
# 把 .env 加入 .gitignore
echo ".env" >> .gitignore
git add .gitignore
git commit -m "加 .env 到 gitignore"

# 如果已经 push 了（敏感信息已在 GitHub 上）：
# 1. 立即更换 API key（已经泄露了）
# 2. 从历史中删除：
git rm --cached .env
echo ".env" >> .gitignore
git commit -m "移除 .env 并加入 gitignore"
git push origin main
```

### Q4：想回到某个历史版本看看

```bash
# 临时切换到某个历史版本（不影响 main）
git checkout 9680dd7                 # 切到这个提交
# 看完了，切回来
git checkout main
```

### Q5：merge 时改了一堆冲突，想放弃合并

```bash
git merge --abort                    # 放弃合并，回到合并前的状态
```

---

## 八、生产环境推荐流程

每次改动都用这个标准流程，最安全：

```bash
# ===== 标准开发流程 =====

# 1. 开始前，拉最新代码
git pull origin main

# 2. 建分支（不在 main 上直接改）
git checkout -b fix/memory-leak

# 3. 改代码 + 测试
# ... 写代码 ...
# ... 跑 pytest ...

# 4. 确认测试通过后提交
git add -A
git commit -m "修复：LongTermMemory 连接未关闭导致内存泄漏"

# 5. 合并到 main
git checkout main
git merge fix/memory-leak

# 6. 推送
git push origin main

# 7. 清理分支
git branch -d fix/memory-leak
```

---

## 快速参考卡

| 操作 | 命令 |
|:--|:--|
| 看改了什么 | `git status` |
| 看具体改动 | `git diff` |
| 加入暂存 | `git add -A` |
| 提交 | `git commit -m "说明"` |
| 推送 | `git push origin main` |
| 拉取 | `git pull origin main` |
| 建分支 | `git checkout -b 分支名` |
| 切换分支 | `git checkout 分支名` |
| 合并分支 | `git merge 分支名` |
| 删分支 | `git branch -d 分支名` |
| 看历史 | `git log --oneline -10` |
| 撤销改动 | `git checkout -- .` |
| 撤销提交（未 push） | `git reset --soft HEAD~1` |
| 撤销提交（已 push） | `git revert HEAD` |
