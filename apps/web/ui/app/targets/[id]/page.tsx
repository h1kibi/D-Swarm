"use client";

import { TargetDetails } from "@/components/Targets/TargetDetails";

interface TargetPageProps {
  params: {
    id: string;
  };
}

export default function TargetPage({ params }: TargetPageProps) {
  return <TargetDetails targetId={params.id} />;
}
