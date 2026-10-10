"use client";

import { EventDetails } from "@/components/Events/EventDetails";

interface EventPageProps {
  params: {
    id: string;
  };
}

export default function EventPage({ params }: EventPageProps) {
  return <EventDetails eventId={params.id} />;
}
