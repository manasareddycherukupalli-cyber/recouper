import React from "react";
import { Composition } from "remotion";
import { DataFlow, TOTAL } from "./DataFlow";

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="DataFlow"
      component={DataFlow}
      durationInFrames={TOTAL}
      fps={30}
      width={1920}
      height={1080}
    />
  );
};
