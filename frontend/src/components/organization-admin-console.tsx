"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ArrowLeft,
  Building2,
  CheckCircle2,
  Fingerprint,
  Loader2,
  LogOut,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  UserRoundCog,
  UsersRound,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { LogoutButton } from "@/components/logout-button";
import { OrganizationExternalIdentities } from "@/components/organization-external-identities";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPut,
} from "@/lib/api";
import type {
  TeamMembershipRole,
  WorkspaceOrganizationRole,
  WorkspaceTeam,
  WorkspaceTeamMember,
  WorkspaceTeamMemberPage,
  WorkspaceTeamPage,
  WorkspaceUser,
  WorkspaceUserPage,
} from "@/types";

type Feedback = { kind: "success" | "error"; message: string } | null;
type ConfirmAction = {
  title: string;
  description: string;
  label: string;
  run: () => Promise<void>;
} | null;

const selectClass =
  "h-11 w-full cursor-pointer rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50";

function message(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function FeedbackAlert({ value }: { value: Feedback }) {
  if (!value) return null;
  const failed = value.kind === "error";
  return (
    <Alert variant={failed ? "destructive" : "default"} aria-live="polite">
      {failed ? <AlertCircle /> : <CheckCircle2 />}
      <AlertTitle>{failed ? "操作失败" : "操作完成"}</AlertTitle>
      <AlertDescription>{value.message}</AlertDescription>
    </Alert>
  );
}

function mergeBy<T>(items: T[], next: T[], key: (item: T) => string) {
  const merged = new Map(items.map((item) => [key(item), item]));
  next.forEach((item) => merged.set(key(item), item));
  return Array.from(merged.values()).sort((a, b) =>
    key(a).localeCompare(key(b)),
  );
}

export function OrganizationAdminConsole({
  organizationId,
}: {
  organizationId: string;
}) {
  const encodedOrg = encodeURIComponent(organizationId);
  const [users, setUsers] = useState<WorkspaceUser[]>([]);
  const [teams, setTeams] = useState<WorkspaceTeam[]>([]);
  const [userCursor, setUserCursor] = useState<string | null>(null);
  const [teamCursor, setTeamCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [loadError, setLoadError] = useState("");
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction>(null);

  const loadDirectory = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const [userPage, teamPage] = await Promise.all([
        apiGet<WorkspaceUserPage>(
          `/api/organizations/${encodedOrg}/users?limit=50`,
        ),
        apiGet<WorkspaceTeamPage>(
          `/api/organizations/${encodedOrg}/teams?limit=50`,
        ),
      ]);
      setUsers(userPage.items);
      setUserCursor(userPage.next_after_user_id);
      setTeams(teamPage.items);
      setTeamCursor(teamPage.next_after_team_id);
    } catch (error) {
      setLoadError(message(error, "组织账号与团队目录加载失败。"));
    } finally {
      setLoading(false);
    }
  }, [encodedOrg]);

  useEffect(() => {
    void loadDirectory();
  }, [loadDirectory]);

  async function loadMoreUsers() {
    if (!userCursor) return;
    setPending("users-more");
    try {
      const page = await apiGet<WorkspaceUserPage>(
        `/api/organizations/${encodedOrg}/users?limit=50&after_user_id=${encodeURIComponent(userCursor)}`,
      );
      setUsers((current) =>
        mergeBy(current, page.items, (item) => item.user_id),
      );
      setUserCursor(page.next_after_user_id);
    } catch (error) {
      setFeedback({ kind: "error", message: message(error, "账号加载失败。") });
    } finally {
      setPending("");
    }
  }

  async function loadMoreTeams() {
    if (!teamCursor) return;
    setPending("teams-more");
    try {
      const page = await apiGet<WorkspaceTeamPage>(
        `/api/organizations/${encodedOrg}/teams?limit=50&after_team_id=${encodeURIComponent(teamCursor)}`,
      );
      setTeams((current) =>
        mergeBy(current, page.items, (item) => item.team_id),
      );
      setTeamCursor(page.next_after_team_id);
    } catch (error) {
      setFeedback({ kind: "error", message: message(error, "团队加载失败。") });
    } finally {
      setPending("");
    }
  }

  return (
    <main className="min-h-dvh bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-6xl flex-col gap-4 px-5 py-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <span className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                <ShieldCheck className="size-4" />
              </span>
              <h1 className="text-xl font-semibold">组织管理</h1>
            </div>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">
              管理本组织的本地账号、登录会话、团队、Team Lead 与外部登录身份。邀请流程仍保持独立。
            </p>
            <p className="mt-1 truncate font-mono text-xs text-muted-foreground" title={organizationId}>
              {organizationId}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button nativeButton={false} variant="outline" className="min-h-11" render={<Link href="/" />}>
              <ArrowLeft />
              返回项目
            </Button>
            <Button type="button" variant="outline" className="min-h-11" disabled={loading || Boolean(pending)} onClick={() => void loadDirectory()}>
              {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              刷新
            </Button>
            <LogoutButton />
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-6xl gap-4 px-5 py-5">
        {loadError && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>组织目录加载失败</AlertTitle>
            <AlertDescription className="grid gap-3">
              <span>{loadError}</span>
              <Button type="button" variant="outline" className="min-h-11 w-fit" onClick={() => void loadDirectory()}>
                <RefreshCw />重试
              </Button>
            </AlertDescription>
          </Alert>
        )}
        <FeedbackAlert value={feedback} />
        {loading && !users.length && !teams.length ? (
          <div className="grid gap-3" role="status" aria-live="polite">
            <span className="sr-only">正在读取组织目录…</span>
            <Skeleton className="h-11 w-full" />
            <Skeleton className="h-72 w-full" />
          </div>
        ) : !loadError || users.length || teams.length ? (
          <Tabs defaultValue="users">
            <TabsList className="min-h-11">
              <TabsTrigger value="users" className="min-h-10">
                <UserRoundCog />账号与会话
              </TabsTrigger>
              <TabsTrigger value="teams" className="min-h-10">
                <Building2 />团队与成员
              </TabsTrigger>
              <TabsTrigger value="identities" className="min-h-10">
                <Fingerprint />外部身份
              </TabsTrigger>
            </TabsList>
            <TabsContent value="users">
              <WorkspaceUsersPanel
                organizationId={organizationId}
                users={users}
                pending={pending}
                setPending={setPending}
                setFeedback={setFeedback}
                setConfirmAction={setConfirmAction}
                refresh={loadDirectory}
              />
              {userCursor && (
                <Button type="button" variant="outline" className="mt-4 min-h-11" disabled={Boolean(pending)} onClick={() => void loadMoreUsers()}>
                  {pending === "users-more" && <Loader2 className="animate-spin" />}
                  加载更多账号
                </Button>
              )}
            </TabsContent>
            <TabsContent value="teams">
              <WorkspaceTeamsPanel
                organizationId={organizationId}
                users={users}
                teams={teams}
                pending={pending}
                setPending={setPending}
                setFeedback={setFeedback}
                setConfirmAction={setConfirmAction}
                refresh={loadDirectory}
              />
              {teamCursor && (
                <Button type="button" variant="outline" className="mt-4 min-h-11" disabled={Boolean(pending)} onClick={() => void loadMoreTeams()}>
                  {pending === "teams-more" && <Loader2 className="animate-spin" />}
                  加载更多团队
                </Button>
              )}
            </TabsContent>
            <TabsContent value="identities">
              <OrganizationExternalIdentities
                organizationId={organizationId}
                users={users}
              />
            </TabsContent>
          </Tabs>
        ) : null}
      </div>

      <Dialog open={confirmAction !== null} onOpenChange={(open) => !open && !pending && setConfirmAction(null)}>
        <DialogContent showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>{confirmAction?.title}</DialogTitle>
            <DialogDescription>{confirmAction?.description}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" className="min-h-11" disabled={Boolean(pending)} />}>取消</DialogClose>
            <Button type="button" variant="destructive" className="min-h-11" disabled={Boolean(pending)} onClick={() => void confirmAction?.run()}>
              {pending ? <Loader2 className="animate-spin" /> : <Trash2 />}
              {confirmAction?.label}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}

type SharedPanelProps = {
  organizationId: string;
  pending: string;
  setPending: (value: string) => void;
  setFeedback: (value: Feedback) => void;
  setConfirmAction: (value: ConfirmAction) => void;
  refresh: () => Promise<void>;
};

function WorkspaceUsersPanel({
  organizationId,
  users,
  pending,
  setPending,
  setFeedback,
  setConfirmAction,
  refresh,
}: SharedPanelProps & { users: WorkspaceUser[] }) {
  const encodedOrg = encodeURIComponent(organizationId);
  const [newUser, setNewUser] = useState({ user_id: "", display_name: "", organization_role: "member" as WorkspaceOrganizationRole });
  const [drafts, setDrafts] = useState<Record<string, { display_name: string; organization_role: WorkspaceOrganizationRole }>>({});

  async function createUser() {
    setPending("user-create");
    setFeedback(null);
    try {
      await apiPost(`/api/organizations/${encodedOrg}/users`, newUser);
      setNewUser({ user_id: "", display_name: "", organization_role: "member" });
      await refresh();
      setFeedback({ kind: "success", message: "本地账号已创建。登录关联仍需单独配置。" });
    } catch (error) {
      setFeedback({ kind: "error", message: message(error, "账号创建失败。") });
    } finally {
      setPending("");
    }
  }

  async function saveUser(user: WorkspaceUser) {
    const draft = drafts[user.user_id] ?? { display_name: user.display_name, organization_role: user.organization_role };
    setPending(`user-${user.user_id}`);
    try {
      await apiPatch(`/api/organizations/${encodedOrg}/users/${encodeURIComponent(user.user_id)}`, draft);
      await refresh();
      setFeedback({ kind: "success", message: `${draft.display_name} 的账号资料已更新。` });
    } catch (error) {
      setFeedback({ kind: "error", message: message(error, "账号更新失败。") });
    } finally {
      setPending("");
    }
  }

  function changeStatus(user: WorkspaceUser) {
    const next = user.status === "active" ? "disabled" : "active";
    const execute = async () => {
      setPending(`status-${user.user_id}`);
      try {
        await apiPatch(`/api/organizations/${encodedOrg}/users/${encodeURIComponent(user.user_id)}`, { status: next });
        setConfirmAction(null);
        await refresh();
        setFeedback({ kind: "success", message: next === "disabled" ? "账号已停用，旧会话立即失效。" : "账号已恢复；停用前会话不会恢复。" });
      } catch (error) {
        setFeedback({ kind: "error", message: message(error, "账号状态更新失败。") });
      } finally {
        setPending("");
      }
    };
    if (next === "disabled") {
      setConfirmAction({
        title: "停用这个账号？",
        description: `将停用 ${user.display_name}，其现有会话立即失效。最后一个 Active 组织管理员不能被停用。`,
        label: "确认停用",
        run: execute,
      });
    } else {
      void execute();
    }
  }

  function revokeSessions(user: WorkspaceUser) {
    setConfirmAction({
      title: "撤销该账号的全部会话？",
      description: `将使 ${user.display_name} 当前所有登录 Cookie 失效，账号本身保持 ${user.status === "active" ? "Active" : "Disabled"}。`,
      label: "撤销会话",
      run: async () => {
        setPending(`sessions-${user.user_id}`);
        try {
          await apiPost(`/api/organizations/${encodedOrg}/users/${encodeURIComponent(user.user_id)}/sessions/revoke`, {});
          setConfirmAction(null);
          setFeedback({ kind: "success", message: "全部现有会话已撤销。" });
        } catch (error) {
          setFeedback({ kind: "error", message: message(error, "会话撤销失败。") });
        } finally {
          setPending("");
        }
      },
    });
  }

  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader className="border-b">
          <CardTitle>创建本地账号</CardTitle>
          <CardDescription>这里只建立 Workspace User，不发送邀请，也不自动关联 OIDC。</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 pt-4 md:grid-cols-[1fr_1fr_180px_auto] md:items-end">
          <div className="grid gap-1.5"><Label htmlFor="new-user-id">User ID</Label><Input id="new-user-id" className="h-11" value={newUser.user_id} onChange={(event) => setNewUser((current) => ({ ...current, user_id: event.target.value }))} /></div>
          <div className="grid gap-1.5"><Label htmlFor="new-user-name">显示名</Label><Input id="new-user-name" className="h-11" value={newUser.display_name} onChange={(event) => setNewUser((current) => ({ ...current, display_name: event.target.value }))} /></div>
          <div className="grid gap-1.5"><Label htmlFor="new-user-role">组织角色</Label><select id="new-user-role" className={selectClass} value={newUser.organization_role} onChange={(event) => setNewUser((current) => ({ ...current, organization_role: event.target.value as WorkspaceOrganizationRole }))}><option value="member">普通成员</option><option value="org_admin">组织管理员</option></select></div>
          <Button type="button" className="min-h-11" disabled={Boolean(pending) || !newUser.user_id.trim() || !newUser.display_name.trim()} onClick={() => void createUser()}>{pending === "user-create" ? <Loader2 className="animate-spin" /> : <Plus />}创建</Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="border-b"><CardTitle>账号目录</CardTitle><CardDescription>登录是否关联只公开为布尔状态；内部 Session Version 不会返回。</CardDescription></CardHeader>
        <CardContent className="grid gap-3 pt-4">
          {users.map((user) => {
            const draft = drafts[user.user_id] ?? { display_name: user.display_name, organization_role: user.organization_role };
            return (
              <div key={user.user_id} className="grid gap-3 rounded-lg border p-3 lg:grid-cols-[minmax(180px,1fr)_minmax(160px,1fr)_180px_auto] lg:items-end">
                <div className="min-w-0"><div className="flex flex-wrap gap-2"><span className="font-medium">{user.display_name}</span><Badge variant={user.status === "active" ? "outline" : "secondary"}>{user.status === "active" ? "Active" : "Disabled"}</Badge><Badge variant="outline">{user.login_linked ? "已关联登录" : "未关联登录"}</Badge></div><p className="mt-1 truncate font-mono text-xs text-muted-foreground" title={user.user_id}>{user.user_id}</p><p className="mt-1 text-xs text-muted-foreground">Team {user.team_membership_count} · Project {user.project_membership_count}</p></div>
                <div className="grid gap-1.5"><Label htmlFor={`name-${user.user_id}`}>显示名</Label><Input id={`name-${user.user_id}`} className="h-11" value={draft.display_name} disabled={Boolean(pending)} onChange={(event) => setDrafts((current) => ({ ...current, [user.user_id]: { ...draft, display_name: event.target.value } }))} /></div>
                <div className="grid gap-1.5"><Label htmlFor={`role-${user.user_id}`}>组织角色</Label><select id={`role-${user.user_id}`} className={selectClass} value={draft.organization_role} disabled={Boolean(pending)} onChange={(event) => setDrafts((current) => ({ ...current, [user.user_id]: { ...draft, organization_role: event.target.value as WorkspaceOrganizationRole } }))}><option value="member">普通成员</option><option value="org_admin">组织管理员</option></select></div>
                <div className="flex flex-wrap gap-2">
                  <Button type="button" className="min-h-11 flex-1" disabled={Boolean(pending) || !draft.display_name.trim()} onClick={() => void saveUser(user)}><Save />保存</Button>
                  <Button type="button" variant="outline" className="min-h-11 flex-1" disabled={Boolean(pending)} onClick={() => revokeSessions(user)}><LogOut />撤销会话</Button>
                  <Button type="button" variant={user.status === "active" ? "destructive" : "outline"} className="min-h-11 flex-1" disabled={Boolean(pending)} onClick={() => changeStatus(user)}>{user.status === "active" ? "停用" : "恢复"}</Button>
                </div>
              </div>
            );
          })}
        </CardContent>
      </Card>
    </div>
  );
}

function WorkspaceTeamsPanel({
  organizationId,
  users,
  teams,
  pending,
  setPending,
  setFeedback,
  setConfirmAction,
  refresh,
}: SharedPanelProps & { users: WorkspaceUser[]; teams: WorkspaceTeam[] }) {
  const encodedOrg = encodeURIComponent(organizationId);
  const activeUsers = useMemo(() => users.filter((user) => user.status === "active"), [users]);
  const [newTeam, setNewTeam] = useState({ team_id: "", name: "", manager_user_id: "" });
  const [selectedTeamId, setSelectedTeamId] = useState<string | null>(null);
  const [members, setMembers] = useState<WorkspaceTeamMember[]>([]);
  const [memberCursor, setMemberCursor] = useState<string | null>(null);
  const [newMemberId, setNewMemberId] = useState("");
  const [newMemberRole, setNewMemberRole] = useState<TeamMembershipRole>("member");
  const selectedTeam = teams.find((team) => team.team_id === selectedTeamId) ?? null;

  async function createTeam() {
    setPending("team-create");
    try {
      await apiPost(`/api/organizations/${encodedOrg}/teams`, { team_id: newTeam.team_id, name: newTeam.name, manager_user_id: newTeam.manager_user_id || null });
      setNewTeam({ team_id: "", name: "", manager_user_id: "" });
      await refresh();
      setFeedback({ kind: "success", message: "团队已创建。Manager 指针不会授予项目访问。" });
    } catch (error) {
      setFeedback({ kind: "error", message: message(error, "团队创建失败。") });
    } finally {
      setPending("");
    }
  }

  async function loadMembers(teamId: string, after?: string) {
    setPending(`team-members-${teamId}`);
    try {
      const page = await apiGet<WorkspaceTeamMemberPage>(`/api/organizations/${encodedOrg}/teams/${encodeURIComponent(teamId)}/members?limit=50${after ? `&after_user_id=${encodeURIComponent(after)}` : ""}`);
      setMembers((current) => after ? mergeBy(current, page.items, (item) => item.user_id) : page.items);
      setMemberCursor(page.next_after_user_id);
      setSelectedTeamId(teamId);
    } catch (error) {
      setFeedback({ kind: "error", message: message(error, "团队成员加载失败。") });
    } finally {
      setPending("");
    }
  }

  async function updateTeam(team: WorkspaceTeam, body: object) {
    setPending(`team-${team.team_id}`);
    try {
      await apiPatch(`/api/organizations/${encodedOrg}/teams/${encodeURIComponent(team.team_id)}`, body);
      await refresh();
      if (selectedTeamId === team.team_id) await loadMembers(team.team_id);
      setFeedback({ kind: "success", message: "团队状态已更新。" });
    } catch (error) {
      setFeedback({ kind: "error", message: message(error, "团队更新失败。") });
    } finally {
      setPending("");
    }
  }

  async function upsertMember(userId: string, role: TeamMembershipRole) {
    if (!selectedTeam) return;
    setPending(`member-${userId}`);
    try {
      await apiPut(`/api/organizations/${encodedOrg}/teams/${encodeURIComponent(selectedTeam.team_id)}/members/${encodeURIComponent(userId)}`, { role });
      await loadMembers(selectedTeam.team_id);
      await refresh();
      setNewMemberId("");
      setFeedback({ kind: "success", message: "团队成员关系已更新。" });
    } catch (error) {
      setFeedback({ kind: "error", message: message(error, "团队成员更新失败。") });
    } finally {
      setPending("");
    }
  }

  function revokeMember(member: WorkspaceTeamMember) {
    if (!selectedTeam) return;
    setConfirmAction({
      title: "撤销团队成员关系？",
      description: `将从 ${selectedTeam.name} 撤销 ${member.display_name} 的 ${member.role}。若其通过该 Team Lead 身份访问项目，权限会立即失效。`,
      label: "确认撤销",
      run: async () => {
        setPending(`member-${member.user_id}`);
        try {
          await apiDelete(`/api/organizations/${encodedOrg}/teams/${encodeURIComponent(selectedTeam.team_id)}/members/${encodeURIComponent(member.user_id)}`);
          setConfirmAction(null);
          await loadMembers(selectedTeam.team_id);
          await refresh();
          setFeedback({ kind: "success", message: "团队成员关系已撤销。" });
        } catch (error) {
          setFeedback({ kind: "error", message: message(error, "撤销失败。") });
        } finally {
          setPending("");
        }
      },
    });
  }

  const candidates = activeUsers.filter((user) => !members.some((member) => member.user_id === user.user_id));
  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader className="border-b"><CardTitle>创建团队</CardTitle><CardDescription>Manager 是管理元数据；需要项目继承权限时仍须授予 Team Lead。</CardDescription></CardHeader>
        <CardContent className="grid gap-3 pt-4 md:grid-cols-[1fr_1fr_1fr_auto] md:items-end">
          <div className="grid gap-1.5"><Label htmlFor="new-team-id">Team ID</Label><Input id="new-team-id" className="h-11" value={newTeam.team_id} onChange={(event) => setNewTeam((current) => ({ ...current, team_id: event.target.value }))} /></div>
          <div className="grid gap-1.5"><Label htmlFor="new-team-name">团队名称</Label><Input id="new-team-name" className="h-11" value={newTeam.name} onChange={(event) => setNewTeam((current) => ({ ...current, name: event.target.value }))} /></div>
          <div className="grid gap-1.5"><Label htmlFor="new-team-manager">Manager（可选）</Label><select id="new-team-manager" className={selectClass} value={newTeam.manager_user_id} onChange={(event) => setNewTeam((current) => ({ ...current, manager_user_id: event.target.value }))}><option value="">未指定</option>{activeUsers.map((user) => <option key={user.user_id} value={user.user_id}>{user.display_name}</option>)}</select></div>
          <Button type="button" className="min-h-11" disabled={Boolean(pending) || !newTeam.team_id.trim() || !newTeam.name.trim()} onClick={() => void createTeam()}><Plus />创建</Button>
        </CardContent>
      </Card>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.9fr)]">
        <Card>
          <CardHeader className="border-b"><CardTitle>团队目录</CardTitle><CardDescription>归档会立即停止 Team Lead 的项目继承权限。</CardDescription></CardHeader>
          <CardContent className="grid gap-3 pt-4">
            {teams.map((team) => (
              <div key={team.team_id} className="grid gap-3 rounded-lg border p-3">
                <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><div className="flex gap-2"><span className="font-medium">{team.name}</span><Badge variant={team.status === "active" ? "outline" : "secondary"}>{team.status === "active" ? "Active" : "Archived"}</Badge></div><p className="mt-1 truncate font-mono text-xs text-muted-foreground">{team.team_id}</p><p className="mt-1 text-xs text-muted-foreground">成员 {team.member_count} · Lead {team.team_lead_count} · 项目 {team.project_count}</p></div><Button type="button" variant="outline" className="min-h-11" disabled={Boolean(pending)} onClick={() => void loadMembers(team.team_id)}><UsersRound />管理成员</Button></div>
                <div className="flex flex-wrap gap-2"><Button type="button" variant={team.status === "active" ? "destructive" : "outline"} className="min-h-11" disabled={Boolean(pending)} onClick={() => team.status === "active" ? setConfirmAction({ title: "归档这个团队？", description: `归档 ${team.name} 会立即停止所有 Team Lead 的项目继承权限，成员历史会保留供清理。`, label: "确认归档", run: async () => { await updateTeam(team, { status: "archived" }); setConfirmAction(null); } }) : void updateTeam(team, { status: "active" })}>{team.status === "active" ? "归档团队" : "恢复团队"}</Button><span className="self-center text-xs text-muted-foreground">Manager: {team.manager_user_id ?? "未指定"}</span></div>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="border-b"><CardTitle>{selectedTeam ? `${selectedTeam.name} · 成员` : "选择团队"}</CardTitle><CardDescription>{selectedTeam ? "只有 Team Lead 产生项目继承；普通 Member 不产生。" : "从左侧选择一个团队管理成员。"}</CardDescription></CardHeader>
          <CardContent className="grid gap-3 pt-4">
            {selectedTeam && (
              <>
                {selectedTeam.status === "active" && (
                  <div className="grid gap-2 rounded-lg border bg-muted/30 p-3">
                    <Label htmlFor="team-candidate">添加 Active 账号</Label>
                    <select id="team-candidate" className={selectClass} value={newMemberId} onChange={(event) => setNewMemberId(event.target.value)}><option value="">选择账号</option>{candidates.map((user) => <option key={user.user_id} value={user.user_id}>{user.display_name}</option>)}</select>
                    <Label htmlFor="team-member-role">团队角色</Label>
                    <select id="team-member-role" className={selectClass} value={newMemberRole} onChange={(event) => setNewMemberRole(event.target.value as TeamMembershipRole)}><option value="member">普通成员</option><option value="team_lead">Team Lead</option></select>
                    <Button type="button" className="min-h-11" disabled={Boolean(pending) || !newMemberId} onClick={() => void upsertMember(newMemberId, newMemberRole)}><Plus />添加成员</Button>
                  </div>
                )}
                {members.map((member) => (
                  <div key={member.user_id} className="grid gap-2 rounded-lg border p-3">
                    <div className="flex flex-wrap justify-between gap-2"><div><span className="font-medium">{member.display_name}</span><p className="font-mono text-xs text-muted-foreground">{member.user_id}</p></div><Badge variant={member.user_status === "active" ? "outline" : "secondary"}>{member.user_status === "active" ? "Active" : "Disabled"}</Badge></div>
                    <div className="flex flex-wrap gap-2"><select aria-label={`${member.display_name} 的团队角色`} className={`${selectClass} flex-1`} value={member.role} disabled={selectedTeam.status === "archived" || member.user_status === "disabled" || Boolean(pending)} onChange={(event) => void upsertMember(member.user_id, event.target.value as TeamMembershipRole)}><option value="member">普通成员</option><option value="team_lead">Team Lead</option></select><Button type="button" variant="destructive" className="min-h-11" disabled={Boolean(pending)} onClick={() => revokeMember(member)}><Trash2 />撤销</Button></div>
                  </div>
                ))}
                {!members.length && <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">暂无团队成员。</div>}
                {memberCursor && <Button type="button" variant="outline" className="min-h-11 justify-self-start" disabled={Boolean(pending)} onClick={() => void loadMembers(selectedTeam.team_id, memberCursor)}><RefreshCw />加载更多成员</Button>}
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
