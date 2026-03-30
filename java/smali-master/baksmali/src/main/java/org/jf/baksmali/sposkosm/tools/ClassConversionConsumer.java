package org.jf.baksmali.sposkosm.tools;

@FunctionalInterface
public interface ClassConversionConsumer {
    void consume(String className, byte[] bytes);
}