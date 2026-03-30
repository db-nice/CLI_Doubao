package org.jf.baksmali.sposkosm;

import org.jf.dexlib2.DexFileFactory;
import org.jf.dexlib2.iface.ClassDef;
import org.jf.dexlib2.iface.DexFile;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

public class DexClassLoader {
    private static final ConcurrentLinkedQueue<String> classNamesQueue = new ConcurrentLinkedQueue<>();
    private static final AtomicBoolean isProcessing = new AtomicBoolean(false);
    private static final AtomicBoolean dataUpdated = new AtomicBoolean(false);
    private static final AtomicInteger processedCount = new AtomicInteger(0);
    private static final AtomicInteger totalCount = new AtomicInteger(0);
    private static final AtomicBoolean taskCompleted = new AtomicBoolean(false);

    private static volatile Thread currentTaskThread = null;

    /**
     * Start class name loading task from Dex file
     */
    public static boolean startClassLoadingFromDex(String dexFilePath) {
        if (isProcessing.get()) {
            stopCurrentTask();
        }
        resetState();
        if (!isProcessing.compareAndSet(false, true)) {
            return false;
        }
        Thread loadThread = new Thread(() -> {
            currentTaskThread = Thread.currentThread();
            try {
                loadClassesFromDexInternal(dexFilePath);
            } catch (IOException e) {
                System.err.println("Failed to load DEX file: " + e.getMessage());
                taskCompleted.set(true);
            } finally {
                isProcessing.set(false);
                currentTaskThread = null;
                taskCompleted.set(true);
            }
        });

        loadThread.setDaemon(true);
        loadThread.start();
        return true;
    }

    /**
     * Internal method: actually load classes from Dex file
     */
    private static void loadClassesFromDexInternal(String dexFilePath) throws IOException {
        File dexFile = new File(dexFilePath);
        if (!dexFile.exists()) {
            throw new IOException("DEX file does not exist: " + dexFilePath);
        }

        try {
            DexFile dex = DexFileFactory.loadDexFile(dexFile, org.jf.dexlib2.Opcodes.forApi(25));
            List<ClassDef> allClasses = new ArrayList<>();
            for (ClassDef classDef : dex.getClasses()) {
                allClasses.add(classDef);
            }

            totalCount.set(allClasses.size());

            for (ClassDef classDef : allClasses) {
                if (Thread.currentThread().isInterrupted()) {
                    return;
                }

                String className = classDef.getType();
                classNamesQueue.offer(className);
                processedCount.incrementAndGet();
                dataUpdated.set(true);
            }

        } catch (Exception e) {
            throw new IOException("Error processing DEX file: " + e.getMessage(), e);
        }
    }

    /**
     * Get loaded class name list - triggers update operation
     */
    public static List<String> getLoadedClassList() {
        List<String> result = new ArrayList<>();
        while (!classNamesQueue.isEmpty()) {
            String className = classNamesQueue.poll();
            if (className != null) {
                result.add(className);
            }
        }
        dataUpdated.set(false);
        return result;
    }

    /**
     * Check if new data is available
     */
    public static boolean hasNewData() {
        return dataUpdated.get() || !classNamesQueue.isEmpty();
    }

    /**
     * Check if task is completed
     */
    public static boolean isTaskCompleted() {
        return taskCompleted.get();
    }

    /**
     * Get processing progress
     */
    public static ProgressInfo getProgress() {
        return new ProgressInfo(processedCount.get(), totalCount.get(), isProcessing.get());
    }

    /**
     * Stop current executing task
     */
    public static void stopCurrentTask() {
        if (isProcessing.get()) {
            isProcessing.set(false);
            if (currentTaskThread != null) {
                currentTaskThread.interrupt();
            }
        }
        resetState();
    }

    /**
     * Reset all states
     */
    private static void resetState() {
        classNamesQueue.clear();
        dataUpdated.set(false);
        processedCount.set(0);
        totalCount.set(0);
        taskCompleted.set(false);
    }

    /**
     * Progress information class
     */
    public static class ProgressInfo {
        private final int processed;
        private final int total;
        private final boolean running;

        public ProgressInfo(int processed, int total, boolean running) {
            this.processed = processed;
            this.total = total;
            this.running = running;
        }

        public int getProcessed() { return processed; }
        public int getTotal() { return total; }
        public boolean isRunning() { return running; }
        public double getPercentage() {
            return total > 0 ? (processed * 100.0 / total) : 0;
        }
    }
}