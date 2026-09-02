import { redirect } from "next/navigation";

export default function OrganizationAdminPage() {
  // Keep old bookmarks working while making Global Settings the single
  // destination for account, model and organization administration.
  redirect("/settings");
}
