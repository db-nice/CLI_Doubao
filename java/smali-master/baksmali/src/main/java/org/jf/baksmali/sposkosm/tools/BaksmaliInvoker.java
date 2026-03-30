package org.jf.baksmali.sposkosm.tools;


//import ma.dexter.api.consumer.ClassProgressConsumer;
//import ma.dexter.dex.MutableClassDef;
import org.jf.baksmali.Adaptors.ClassDefinition;
import org.jf.baksmali.BaksmaliOptions;
import org.jf.baksmali.formatter.BaksmaliWriter;
import org.jf.dexlib2.DexFileFactory;
import org.jf.dexlib2.iface.ClassDef;

import java.io.File;
import java.io.IOException;
import java.io.StringWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

public class BaksmaliInvoker {

    private final BaksmaliOptions baksmaliOptions;

    public BaksmaliInvoker() {
        this(new BaksmaliOptions());
    }

    public BaksmaliInvoker(BaksmaliOptions baksmaliOptions) {
        this.baksmaliOptions = baksmaliOptions;
    }

    /**
     * Disassemble a MutableClassDef to Smali code
     */
    public String disassemble(MutableClassDef classDef) throws IOException {
        return disassemble(classDef.getClassDef());
    }

    /**
     * Disassemble a ClassDef to Smali code
     */
    public String disassemble(ClassDef classDef) throws IOException {
        StringWriter writer = new StringWriter();
        ClassDefinition classDefinition = new ClassDefinition(baksmaliOptions, classDef);
        classDefinition.writeTo(new BaksmaliWriter(writer));
        return writer.toString();
    }

    /**
     * Disassemble an entire DEX file to a ZIP of Smali files
     */
    public void disassemble(File dexFile, File outZip, ClassProgressConsumer progressConsumer) {
        try {
            // Load classes from DEX file
            List<ClassDef> classDefs = new ArrayList<>(
                    DexFileFactory.loadDexFile(dexFile, null).getClasses()
            );

            AtomicInteger currentClassCount = new AtomicInteger();
            int totalClassCount = classDefs.size();

            try (ZipOutputStream zos = new ZipOutputStream(new java.io.FileOutputStream(outZip))) {
                for (ClassDef classDef : classDefs) {
                    if (classDef == null) continue;

                    String smaliPath = normalizeSmaliPath(classDef.getType());
                    progressConsumer.consume(
                            smaliPath,
                            currentClassCount.incrementAndGet(),
                            totalClassCount
                    );

                    ZipEntry zipEntry = new ZipEntry(smaliPath + ".smali");
                    zos.putNextEntry(zipEntry);

                    String smaliCode = disassemble(classDef);
                    zos.write(smaliCode.getBytes(StandardCharsets.UTF_8));

                    zos.closeEntry();
                }
            }
        } catch (Exception e) {
            throw new RuntimeException("Failed to disassemble DEX file", e);
        }
    }

    public BaksmaliOptions getBaksmaliOptions() {
        return baksmaliOptions;
    }

    private static String normalizeSmaliPath(String classDefType) {
        if (classDefType.startsWith("L") && classDefType.endsWith(";")) {
            return classDefType.substring(1, classDefType.length() - 1);
        }
        return classDefType;
    }
}
