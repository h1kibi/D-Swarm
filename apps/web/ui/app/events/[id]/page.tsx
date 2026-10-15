"use client";

import { use } from "react";

import { EventDetails } from "@/components/Events/EventDetails";

interface EventPageProps {
  // Next.js 15: route params arrive as a Promise in client components too.
  params: Promise<{
    id: string;
  }>;
}

export default function EventPage({ params }: EventPageProps) {
  const { id } = use(params);
  return <EventDetails eventId={id} />;
}
