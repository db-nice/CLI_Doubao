package org.jf.baksmali.sposkosm;

import java.util.List;

public class DexClassLoaderExample {

    private static final boolean SHOW_DETAILED_CLASSES = false;
    private static final int MAX_CLASSES_TO_SHOW = 5;

    public static void main(String[] args) {
        long startTime = System.currentTimeMillis();
        int totalClassesProcessed = 0;

        try {
            System.out.println("System encoding: " + System.getProperty("file.encoding"));
            System.out.println("Starting DEX class loader...");

            boolean started = DexClassLoader.startClassLoadingFromDex(
                    "F:\\Project\\Android\\exnop\\dex-editor-master\\smali-master\\build\\dex\\classes.dex");

            if (started) {
                System.out.println("Class loading task started");
                System.out.println();

                int lastProgress = -1;
                boolean firstBatch = true;

                while (!DexClassLoader.isTaskCompleted() || DexClassLoader.hasNewData()) {
                    if (DexClassLoader.hasNewData()) {
                        List<String> newClasses = DexClassLoader.getLoadedClassList();
                        if (!newClasses.isEmpty()) {
                            totalClassesProcessed += newClasses.size();

                            if (SHOW_DETAILED_CLASSES) {
                                System.out.println("Acquired " + newClasses.size() + " new classes:");
                                for (String className : newClasses) {
                                    System.out.println("  " + className);
                                }
                            } else {
                                if (firstBatch) {
                                    System.out.println("Classes are being loaded in background...");
                                    System.out.println("(Set SHOW_DETAILED_CLASSES=true to see all class names)");
                                    firstBatch = false;
                                }

                                if (newClasses.size() <= MAX_CLASSES_TO_SHOW) {
                                    System.out.println("Acquired " + newClasses.size() + " new classes");
                                    for (String className : newClasses) {
                                        System.out.println("  " + className);
                                    }
                                } else {
                                    System.out.println("Acquired " + newClasses.size() + " new classes (showing first " + MAX_CLASSES_TO_SHOW + ")");
                                    for (int i = 0; i < MAX_CLASSES_TO_SHOW; i++) {
                                        System.out.println("  " + newClasses.get(i));
                                    }
                                    System.out.println("  ... and " + (newClasses.size() - MAX_CLASSES_TO_SHOW) + " more");
                                }
                            }
                            System.out.println();
                        }
                    }

                    DexClassLoader.ProgressInfo progress = DexClassLoader.getProgress();
                    if (progress.getProcessed() != lastProgress) {
                        printProgress(progress.getProcessed(), progress.getTotal());
                        lastProgress = progress.getProcessed();
                    }

                }

                DexClassLoader.ProgressInfo finalProgress = DexClassLoader.getProgress();
                printProgress(finalProgress.getTotal(), finalProgress.getTotal());

                long endTime = System.currentTimeMillis();
                long duration = endTime - startTime;

                System.out.println("=== LOADING COMPLETED ===");
                System.out.println("Total classes loaded: " + totalClassesProcessed);
                System.out.println("Time elapsed: " + duration + "ms");
                System.out.println("Average speed: " + (duration > 0 ?
                        String.format("%.2f", totalClassesProcessed * 1000.0 / duration) : "N/A") + " classes/second");

            } else {
                System.out.println("Failed to start class loading task");
            }

        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    /**
     * Visual progress display
     */
    private static void printProgress(int current, int total) {
        if (total <= 0) return;

        int width = 50;
        int progress = (int) ((double) current / total * width);
        int percentage = (int) ((double) current / total * 100);

        System.out.print("\r[");
        for (int i = 0; i < width; i++) {
            if (i < progress) System.out.print("=");
            else if (i == progress) System.out.print(">");
            else System.out.print(" ");
        }
        System.out.print("] " + current + "/" + total + " (" + percentage + "%)");

        if (current == total) {
        }
    }
}