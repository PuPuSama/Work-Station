"use client";

import { LogOut } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { apiGet, apiPost } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { ApiMessage } from "@/types";

type AuthStatus = {
  message: string;
  data?: {
    enabled?: boolean;
    authenticated?: boolean;
  };
};

export function LogoutButton({
  iconOnly = false,
  className,
}: {
  iconOnly?: boolean;
  className?: string;
}) {
  const [visible, setVisible] = useState(false);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    apiGet<AuthStatus>("/api/auth/status")
      .then((status) => {
        setVisible(
          Boolean(status.data?.enabled && status.data?.authenticated),
        );
      })
      .catch(() => setVisible(false));
  }, []);

  if (!visible) return null;

  async function logout() {
    setPending(true);
    try {
      await apiPost<ApiMessage>("/api/auth/logout");
    } finally {
      window.location.assign("/login");
    }
  }

  return (
    <Button
      type="button"
      size={iconOnly ? "icon-sm" : "sm"}
      variant="ghost"
      className={cn(className)}
      disabled={pending}
      aria-label={iconOnly ? "退出登录" : undefined}
      onClick={() => void logout()}
    >
      <LogOut />
      {!iconOnly && (pending ? "正在退出" : "退出登录")}
    </Button>
  );
}
