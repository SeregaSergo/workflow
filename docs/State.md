```json
{
  "task_id": "ABC-123",
  "workflow": "coding",
  "repo_path": "/home/u/work/teams/team1/services/foo",
  "worktree": "/home/u/work/teams/team1/services/foo-ABC-123",
  "task_url": "https://youtrack.wildberries.ru/issue/ABC-123",
  "branch": "ABC-123-fix-x",
  "base_branch": "master",
  "current_stage": "plan",
  "iteration": 2,
  "pause_consumed_for": null,
  "created_at": "...",
  "updated_at": "..."
}
```

`pause_consumed_for: str | null` — служебный флаг для конфигурируемых пауз
(`workflows.<workflow>.pause_before` в конфиге). Если фаза `N` указана в
`pause_before`, движок при входе в неё проставляет `pause_consumed_for = N`,
сейвит state и выходит с exit code 3. `wf resume` повторно вызывает `step()`,
видит `pause_consumed_for == current_stage`, пропускает паузу и запускает фазу.
После любого перехода (`current_stage` изменился) поле сбрасывается в `null`,
поэтому в циклах `review → plan → … → N` пауза перед `N` сработает на каждой
итерации.
