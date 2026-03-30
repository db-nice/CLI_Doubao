/*
 * Copyright 2026
 *
 * Utility for parsing smali-style method descriptors.
 */
package org.jf.baksmali.analysis;

import org.jf.dexlib2.immutable.reference.ImmutableMethodReference;

import javax.annotation.Nonnull;
import java.util.ArrayList;
import java.util.List;

public final class MethodDescriptorParser {
    private MethodDescriptorParser() {
    }

    @Nonnull
    public static ImmutableMethodReference parse(@Nonnull String descriptor) {
        String trimmed = descriptor.trim();
        if (trimmed.isEmpty()) {
            throw new IllegalArgumentException("Empty method descriptor");
        }

        int arrow = trimmed.indexOf("->");
        if (arrow < 0) {
            throw new IllegalArgumentException("Method descriptor must contain \"->\"");
        }

        String definingClass = trimmed.substring(0, arrow).trim();
        if (definingClass.isEmpty()) {
            throw new IllegalArgumentException("Missing defining class in method descriptor");
        }

        int nameStart = arrow + 2;
        int paramsStart = trimmed.indexOf('(', nameStart);
        if (paramsStart < 0) {
            throw new IllegalArgumentException("Missing '(' in method descriptor");
        }

        String name = trimmed.substring(nameStart, paramsStart).trim();
        if (name.isEmpty()) {
            throw new IllegalArgumentException("Missing method name in method descriptor");
        }

        int paramsEnd = trimmed.indexOf(')', paramsStart);
        if (paramsEnd < 0) {
            throw new IllegalArgumentException("Missing ')' in method descriptor");
        }

        String paramsDescriptor = trimmed.substring(paramsStart + 1, paramsEnd);
        String returnDescriptor = trimmed.substring(paramsEnd + 1).trim();
        if (returnDescriptor.isEmpty()) {
            throw new IllegalArgumentException("Missing return type in method descriptor");
        }

        List<String> params = parseParameterTypes(paramsDescriptor);
        String returnType = parseReturnType(returnDescriptor);

        return new ImmutableMethodReference(definingClass, name, params, returnType);
    }

    @Nonnull
    private static List<String> parseParameterTypes(@Nonnull String paramsDescriptor) {
        List<String> params = new ArrayList<String>();
        int index = 0;
        while (index < paramsDescriptor.length()) {
            int next = parseType(paramsDescriptor, index, false);
            params.add(paramsDescriptor.substring(index, next));
            index = next;
        }
        return params;
    }

    @Nonnull
    private static String parseReturnType(@Nonnull String returnDescriptor) {
        int end = parseType(returnDescriptor, 0, true);
        if (end != returnDescriptor.length()) {
            throw new IllegalArgumentException("Extra characters after return type");
        }
        return returnDescriptor.substring(0, end);
    }

    private static int parseType(@Nonnull String descriptor, int start, boolean allowVoid) {
        if (start >= descriptor.length()) {
            throw new IllegalArgumentException("Unexpected end of type descriptor");
        }

        char c = descriptor.charAt(start);
        if (c == '[') {
            int index = start;
            while (index < descriptor.length() && descriptor.charAt(index) == '[') {
                index++;
            }
            return parseType(descriptor, index, false);
        }

        if (c == 'L') {
            int end = descriptor.indexOf(';', start + 1);
            if (end < 0) {
                throw new IllegalArgumentException("Unterminated object type in descriptor");
            }
            return end + 1;
        }

        if (c == 'V') {
            if (!allowVoid) {
                throw new IllegalArgumentException("Void type is not allowed here");
            }
            return start + 1;
        }

        if (isPrimitive(c)) {
            return start + 1;
        }

        throw new IllegalArgumentException("Invalid type descriptor: " + descriptor.substring(start));
    }

    private static boolean isPrimitive(char c) {
        switch (c) {
            case 'Z':
            case 'B':
            case 'S':
            case 'C':
            case 'I':
            case 'J':
            case 'F':
            case 'D':
                return true;
            default:
                return false;
        }
    }
}
