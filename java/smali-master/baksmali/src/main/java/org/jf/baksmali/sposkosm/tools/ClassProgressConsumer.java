package org.jf.baksmali.sposkosm.tools;

@FunctionalInterface
public interface ClassProgressConsumer {
    void consume(String className, int current, int total);
}