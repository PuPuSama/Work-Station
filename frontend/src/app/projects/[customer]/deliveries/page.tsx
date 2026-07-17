import { ProjectDeliveryRecords } from "@/components/project-delivery-records";

export default async function DeliveriesPage({ params }: { params: Promise<{ customer: string }> }) {
  const { customer } = await params;
  return <ProjectDeliveryRecords customer={decodeURIComponent(customer)} />;
}
